"""Tests for the social proof app.

Covers the live-viewer counter (Redis semantics emulated on the test cache),
the bucketed durable event log with its concurrency-safe deduplication, the
cached feature-flag gate and its signal-driven invalidation, the retention
purge task, and every public endpoint: single and batch viewer counts, the
recent-sales feed, plus the security invariants — cookie-only identity,
ignored client-supplied session keys, bot-traffic exclusion, CSRF-exempt
recording, and the concurrent-views scenario the feature is designed for.
"""

import time
from datetime import timedelta
from decimal import Decimal
from threading import Thread
from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.catalog.models import Product
from apps.core.models import SiteConfig
from apps.orders.models import Order, OrderItem
from apps.social_proof import cache as proof_cache
from apps.social_proof.constants import (
    RECENT_SALES_WINDOW_HOURS,
    VIEW_EVENT_RETENTION_DAYS,
    VISITOR_COOKIE,
)
from apps.social_proof.models import ProductViewEvent
from apps.social_proof.services import (
    _bucket_start,
    get_live_viewer_count,
    record_product_view,
)
from apps.social_proof.tasks import purge_old_view_events_task

_PRODUCT_SEQ = [0]

_DEFAULT_DESCRIPTION = "A test appliance."


def _make_product(**kwargs):
    """Create an active product with unique slug and sku."""
    _PRODUCT_SEQ[0] += 1
    n = _PRODUCT_SEQ[0]
    return Product.objects.create(
        name=kwargs.pop("name", f"Appliance {n}"),
        slug=kwargs.pop("slug", f"appliance-{n}"),
        sku=kwargs.pop("sku", f"APP-{n}"),
        description=kwargs.pop("description", _DEFAULT_DESCRIPTION),
        is_active=kwargs.pop("is_active", True),
        is_discontinued=kwargs.pop("is_discontinued", False),
        **kwargs,
    )


def _make_order(status="confirmed"):
    """Create an order line with the minimal money fields a purchase needs."""
    return Order.objects.create(
        phone="+254712345678",
        email="buyer@example.com",
        status=status,
        currency="KES",
        subtotal=Decimal("100.00"),
        shipping_total=Decimal("0.00"),
        tax_total=Decimal("16.00"),
        grand_total=Decimal("116.00"),
    )


def _make_order_item(order, product):
    """Create an order line for a product, snapshotting order-time data."""
    return OrderItem.objects.create(
        order=order,
        product=product,
        variant_sku="APP-SKU",
        product_name=product.name,
        unit_price=Decimal("100.00"),
        quantity=1,
        total_price=Decimal("100.00"),
        tax_rate=Decimal("16.00"),
    )


class SocialProofTestCase(APITestCase):
    """Base case that starts each test with a clean feature flag and cache."""

    def setUp(self):
        """Reset flag defaults and the shared cache for a fresh test."""
        cache.clear()
        site_config = SiteConfig.load()
        site_config.settings = {**site_config.settings, "enable_social_proof": True}
        site_config.save()


class LiveViewerCacheTests(SocialProofTestCase):
    """Exercises the Redis-backed live-viewer counter helpers."""

    def test_distinct_sessions_count_once_each(self):
        """Adding distinct sessions yields a count equal to their number."""
        for index in range(5):
            proof_cache.add_live_viewer(1, f"session-{index}")
        self.assertEqual(proof_cache.get_live_viewer_count(1), 5)

    def test_same_session_re_added_counts_once(self):
        """The same session added repeatedly still counts once."""
        for _ in range(10):
            proof_cache.add_live_viewer(1, "session-a")
        self.assertEqual(proof_cache.get_live_viewer_count(1), 1)

    def test_counts_are_per_product(self):
        """Live-viewer counts never bleed between products."""
        proof_cache.add_live_viewer(1, "session-a")
        proof_cache.add_live_viewer(1, "session-b")
        proof_cache.add_live_viewer(2, "session-a")
        self.assertEqual(proof_cache.get_live_viewer_count(1), 2)
        self.assertEqual(proof_cache.get_live_viewer_count(2), 1)


class LiveViewerCountBatchCacheTests(SocialProofTestCase):
    """Exercises the batched live-viewer counter read."""

    def test_batch_returns_count_per_product(self):
        """Each product gets its own count from one batch call."""
        proof_cache.add_live_viewer(1, "session-a")
        proof_cache.add_live_viewer(2, "session-b")
        proof_cache.add_live_viewer(2, "session-c")
        counts = proof_cache.get_live_viewer_count_batch([1, 2])
        self.assertEqual(counts, {1: 1, 2: 2})

    def test_batch_ignores_duplicate_product_ids(self):
        """Requesting a product twice still returns it exactly once."""
        proof_cache.add_live_viewer(3, "session-a")
        counts = proof_cache.get_live_viewer_count_batch([3, 3, 3])
        self.assertEqual(counts, {3: 1})

    def test_batch_with_no_ids_returns_empty(self):
        """An empty request yields an empty mapping."""
        self.assertEqual(proof_cache.get_live_viewer_count_batch([]), {})


class RecordViewServiceTests(SocialProofTestCase):
    """Exercises the service-level record and count logic."""

    def setUp(self):
        super().setUp()
        _PRODUCT_SEQ[0] += 1
        self.product = _make_product(name="Kettle", slug="kettle")

    def test_recording_creates_durable_event_and_counts_viewer(self):
        """A recorded view appends an event row and one live viewer."""
        count = record_product_view(self.product, session_key="viewer-a")
        self.assertEqual(count, 1)
        self.assertEqual(get_live_viewer_count(self.product), 1)
        self.assertEqual(ProductViewEvent.objects.count(), 1)
        event = ProductViewEvent.objects.first()
        self.assertEqual(event.product, self.product)
        self.assertEqual(event.session_key, "viewer-a")
        self.assertIsNotNone(event.bucket_started_at)

    def test_repeated_views_by_one_session_keep_one_event_and_viewer(self):
        """Rapid reloads by one session collapse to one event row."""
        for _ in range(3):
            record_product_view(self.product, session_key="viewer-a")
        self.assertEqual(get_live_viewer_count(self.product), 1)
        self.assertEqual(ProductViewEvent.objects.count(), 1)

    def test_views_in_separate_buckets_each_append_an_event(self):
        """Views in different buckets each leave their own history row."""
        base = timezone.now().replace(minute=0, second=0, microsecond=0)
        bucket_times = [base + timedelta(minutes=5 * index) for index in range(3)]
        with patch(
            "apps.social_proof.services._bucket_start",
            side_effect=bucket_times,
        ):
            for _ in range(3):
                record_product_view(self.product, session_key="viewer-a")
        self.assertEqual(get_live_viewer_count(self.product), 1)
        self.assertEqual(ProductViewEvent.objects.count(), 3)

    def test_distinct_sessions_each_count(self):
        """Ten distinct sessions produce ten live viewers and ten events."""
        for index in range(10):
            record_product_view(self.product, session_key=f"viewer-{index}")
        self.assertEqual(get_live_viewer_count(self.product), 10)
        self.assertEqual(ProductViewEvent.objects.count(), 10)

    def test_session_key_generated_when_omitted(self):
        """A view without a session identifier still records an event."""
        count = record_product_view(self.product)
        self.assertEqual(count, 1)
        event = ProductViewEvent.objects.first()
        self.assertTrue(event.session_key)

    def test_bucket_start_aligns_to_five_minutes(self):
        """Bucket starts always land on multiples of five minutes."""
        moment = timezone.now().replace(minute=7, second=30, microsecond=400000)
        self.assertEqual(_bucket_start(moment).minute, 5)
        self.assertEqual(_bucket_start(moment).second, 0)
        self.assertEqual(_bucket_start(moment).microsecond, 0)
        exact = timezone.now().replace(minute=10, second=0, microsecond=0)
        self.assertEqual(_bucket_start(exact).minute, 10)

    def test_disabled_feature_records_nothing(self):
        """With social proof switched off, views are dropped entirely."""
        site_config = SiteConfig.load()
        site_config.settings = {**site_config.settings, "enable_social_proof": False}
        site_config.save()
        count = record_product_view(self.product, session_key="viewer-a")
        self.assertEqual(count, 0)
        self.assertEqual(get_live_viewer_count(self.product), 0)
        self.assertEqual(ProductViewEvent.objects.count(), 0)


class ConcurrentViewSimulationTests(SocialProofTestCase):
    """Exercises the concurrent-views scenario the feature is built for."""

    def setUp(self):
        super().setUp()
        _PRODUCT_SEQ[0] += 1
        self.product = _make_product(name="Fridge", slug="fridge")

    def test_concurrent_distinct_viewers_count_each(self):
        """Concurrent adds from distinct sessions are never lost."""
        product_pk = self.product.pk
        threads = [
            Thread(target=proof_cache.add_live_viewer, args=(product_pk, f"s{i}"))
            for i in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(proof_cache.get_live_viewer_count(product_pk), 20)

    def test_concurrent_duplicate_sessions_stay_a_single_viewer(self):
        """Concurrent re-arms by one session never inflate the count."""
        product_pk = self.product.pk
        threads = [
            Thread(target=proof_cache.add_live_viewer, args=(product_pk, "same-s"))
            for _ in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(proof_cache.get_live_viewer_count(product_pk), 1)

    def test_many_distinct_sessions_recorded_together(self):
        """Simulating a view spike records distinct, durable events."""
        for index in range(25):
            record_product_view(self.product, session_key=f"burst-{index}")
        self.assertEqual(get_live_viewer_count(self.product), 25)
        self.assertEqual(
            ProductViewEvent.objects.filter(product=self.product).count(), 25
        )


class ProductViewApiTests(SocialProofTestCase):
    """Exercises the public view-recording endpoint and its identity rules."""

    def setUp(self):
        super().setUp()
        self.product = _make_product(name="Microwave", slug="microwave")
        self.record_url = reverse("api:social_proof:product-view", args=["microwave"])
        self.viewers_url = reverse(
            "api:social_proof:product-viewers", args=["microwave"]
        )

    def test_anonymous_can_record_a_view(self):
        """Recording a view is open to any caller and mints a visitor cookie."""
        response = self.client.post(self.record_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["live_viewers"], 1)
        self.assertIn(VISITOR_COOKIE, response.cookies)

    def test_anonymous_can_read_viewer_count(self):
        """Reading the live count is open to any caller."""
        response = self.client.get(self.viewers_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["live_viewers"], 0)

    def test_repeated_views_by_same_visitor_count_once(self):
        """The visitor cookie keeps one caller counting as one viewer."""
        self.client.post(self.record_url, {}, format="json")
        self.client.post(self.record_url, {}, format="json")
        response = self.client.get(self.viewers_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["live_viewers"], 1)

    def test_distinct_visitors_each_count(self):
        """Callers minted separate visitor cookies count separately."""
        self.client.post(self.record_url, {}, format="json")
        self.client.cookies.pop(VISITOR_COOKIE, None)
        self.client.post(self.record_url, {}, format="json")
        response = self.client.get(self.viewers_url)
        self.assertEqual(response.data["live_viewers"], 2)
        keys = list(ProductViewEvent.objects.values_list("session_key", flat=True))
        self.assertEqual(len(set(keys)), 2)

    def test_supplied_session_key_is_ignored(self):
        """A forged session key in the body never becomes the identity."""
        response = self.client.post(
            self.record_url, {"session_key": "forged-viewer"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["live_viewers"], 1)
        self.assertEqual(
            ProductViewEvent.objects.filter(session_key="forged-viewer").count(), 0
        )
        event = ProductViewEvent.objects.first()
        self.assertEqual(event.session_key, response.cookies[VISITOR_COOKIE].value)

    def test_oversized_visitor_cookie_is_replaced(self):
        """A malformed oversized cookie is discarded for a fresh identity."""
        self.client.cookies[VISITOR_COOKIE] = "x" * 101
        response = self.client.post(self.record_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        new_key = response.cookies[VISITOR_COOKIE].value
        self.assertLessEqual(len(new_key), 100)
        event = ProductViewEvent.objects.first()
        self.assertEqual(event.session_key, new_key)

    def test_bot_user_agent_is_not_recorded(self):
        """Bot traffic is acknowledged but never recorded or minted a cookie."""
        self.client.post(self.record_url, {}, format="json")
        response = self.client.post(
            self.record_url,
            {},
            format="json",
            HTTP_USER_AGENT="Mozilla/5.0 (compatible; Googlebot/2.1)",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["live_viewers"], 1)
        self.assertEqual(ProductViewEvent.objects.count(), 1)

    def test_recording_is_csrf_exempt_for_authenticated_storefront(self):
        """A CSRF-enforcing client can still record on the storefront path."""
        csrf_client = APIClient(enforce_csrf_checks=True)
        response = csrf_client.post(self.record_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_record_missing_product_is_404(self):
        """Recording a view for a vanished product is a 404."""
        url = reverse("api:social_proof:product-view", args=["missing"])
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_record_inactive_product_is_404(self):
        """An inactive product cannot have views recorded against it."""
        hidden = _make_product(slug="hidden", is_active=False)
        url = reverse("api:social_proof:product-view", args=[hidden.slug])
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_viewers_missing_product_is_404(self):
        """Reading viewers for a vanished product is a 404."""
        url = reverse("api:social_proof:product-viewers", args=["missing"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_disabled_feature_returns_zero_and_records_nothing(self):
        """With the flag off the endpoints respond 0 and store nothing."""
        site_config = SiteConfig.load()
        site_config.settings = {**site_config.settings, "enable_social_proof": False}
        site_config.save()
        response = self.client.post(self.record_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["live_viewers"], 0)
        self.assertEqual(ProductViewEvent.objects.count(), 0)
        read = self.client.get(self.viewers_url)
        self.assertEqual(read.data["live_viewers"], 0)


class ViewerCountsBatchApiTests(SocialProofTestCase):
    """Exercises the batched viewer-count endpoint."""

    def setUp(self):
        super().setUp()
        self.product_a = _make_product(name="Microwave", slug="microwave")
        self.product_b = _make_product(name="Kettle", slug="kettle")
        self.url = reverse("api:social_proof:viewer-counts-batch")

    def test_counts_for_multiple_products(self):
        """The endpoint returns one count per requested visible product."""
        record_product_view(self.product_a, session_key="visitor-a")
        record_product_view(self.product_b, session_key="visitor-b")
        response = self.client.get(
            self.url, {"products": "microwave,kettle"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["live_viewers"], {"microwave": 1, "kettle": 1})

    def test_unviewed_product_returns_zero(self):
        """A product nobody is viewing still appears with a zero count."""
        record_product_view(self.product_a, session_key="visitor-a")
        response = self.client.get(
            self.url, {"products": "microwave,kettle"}, format="json"
        )
        self.assertEqual(response.data["live_viewers"], {"microwave": 1, "kettle": 0})

    def test_missing_or_hidden_product_slugs_are_omitted(self):
        """Slugs that resolve to nothing are filtered out of the response."""
        hidden = _make_product(slug="hidden", is_active=False)
        response = self.client.get(
            self.url, {"products": f"microwave,{hidden.slug},missing"}, format="json"
        )
        self.assertEqual(response.data["live_viewers"], {"microwave": 0})

    def test_missing_products_param_is_rejected(self):
        """The ``products`` query parameter is required."""
        response = self.client.get(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_products_param_is_rejected(self):
        """An empty ``products`` value cannot fan out to zero products."""
        response = self.client.get(self.url, {"products": " , "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_too_many_slugs_are_rejected(self):
        """More than the configured cap of slugs is a validation error."""
        slugs = ",".join(f"slug-{index}" for index in range(51))
        response = self.client.get(self.url, {"products": slugs}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_feature_returns_zero_counts(self):
        """With the flag off every requested product reports zero viewers."""
        site_config = SiteConfig.load()
        site_config.settings = {**site_config.settings, "enable_social_proof": False}
        site_config.save()
        response = self.client.get(
            self.url, {"products": "microwave,kettle"}, format="json"
        )
        self.assertEqual(response.data["live_viewers"], {"microwave": 0, "kettle": 0})


class RecentSalesApiTests(SocialProofTestCase):
    """Exercises the recent-sales feed endpoint."""

    def setUp(self):
        super().setUp()
        self.product = _make_product(name="Toaster", slug="toaster")
        self.url = reverse("api:social_proof:recent-sales")

    def test_confirmed_purchase_appears_in_feed(self):
        """A completed order's line shows up with the product name."""
        order = _make_order(status="confirmed")
        _make_order_item(order, self.product)
        response = self.client.get(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sales = response.data["recent_sales"]
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["product_slug"], "toaster")
        self.assertEqual(sales[0]["product_name"], "Toaster")
        self.assertEqual(sales[0]["quantity"], 1)

    def test_unfulfilled_orders_are_excluded(self):
        """Pending, cancelled, and refunded orders never surface."""
        for order_status in ("pending", "cancelled", "refunded"):
            order = _make_order(status=order_status)
            _make_order_item(order, self.product)
        response = self.client.get(self.url, {}, format="json")
        self.assertEqual(response.data["recent_sales"], [])

    def test_purchases_outside_the_window_are_excluded(self):
        """Purchases older than the configured window are filtered out."""
        order = _make_order(status="confirmed")
        _make_order_item(order, self.product)
        Order.objects.filter(pk=order.pk).update(
            placed_at=timezone.now() - timedelta(hours=RECENT_SALES_WINDOW_HOURS + 1)
        )
        response = self.client.get(self.url, {}, format="json")
        self.assertEqual(response.data["recent_sales"], [])

    def test_limit_is_respected(self):
        """The limit caps how many lines the feed returns."""
        for index in range(3):
            order = _make_order(status="confirmed")
            _make_order_item(
                order, _make_product(name=f"Fan {index}", slug=f"fan-{index}")
            )
        response = self.client.get(self.url, {"limit": 1}, format="json")
        self.assertEqual(len(response.data["recent_sales"]), 1)

    def test_feed_exposes_no_customer_data(self):
        """Responses carry only product, quantity, and time fields."""
        order = _make_order(status="confirmed")
        _make_order_item(order, self.product)
        response = self.client.get(self.url, {}, format="json")
        sale = response.data["recent_sales"][0]
        self.assertEqual(
            set(sale.keys()), {"product_slug", "product_name", "quantity", "placed_at"}
        )

    def test_invalid_limit_is_rejected(self):
        """Limits out of range are validation errors."""
        for limit in ("0", "99", "abc"):
            response = self.client.get(self.url, {"limit": limit}, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_feature_returns_empty_feed(self):
        """With the flag off the feed is empty."""
        site_config = SiteConfig.load()
        site_config.settings = {**site_config.settings, "enable_social_proof": False}
        site_config.save()
        response = self.client.get(self.url, {}, format="json")
        self.assertEqual(response.data["recent_sales"], [])


class FeatureFlagGateTests(SocialProofTestCase):
    """Exercises the cached feature flag and its invalidation."""

    def test_site_config_save_invalidates_cached_flag(self):
        """Saving the configuration immediately refreshes the cached flag."""
        self.assertTrue(proof_cache.is_feature_enabled())
        site_config = SiteConfig.load()
        site_config.settings = {**site_config.settings, "enable_social_proof": False}
        site_config.save()
        self.assertFalse(proof_cache.is_feature_enabled())

    def test_flag_is_served_from_cache_until_invalidated(self):
        """Without invalidation the cached value is served as-is."""
        self.assertTrue(proof_cache.is_feature_enabled())
        SiteConfig.objects.filter(pk=1).update(
            settings={
                **SiteConfig.load().settings,
                "enable_social_proof": False,
            }
        )
        self.assertTrue(proof_cache.is_feature_enabled())
        proof_cache.invalidate_feature_enabled()
        self.assertFalse(proof_cache.is_feature_enabled())


class RetentionPurgeTaskTests(SocialProofTestCase):
    """Exercises the daily event-purge maintenance task."""

    def setUp(self):
        super().setUp()
        _PRODUCT_SEQ[0] += 1
        self.product = _make_product(name="Blender", slug="blender")

    def test_purge_removes_only_events_older_than_retention(self):
        """Only rows past the retention window are deleted."""
        record_product_view(self.product, session_key="recent-viewer")
        record_product_view(self.product, session_key="old-viewer")
        ProductViewEvent.objects.filter(session_key="old-viewer").update(
            created_at=timezone.now() - timedelta(days=VIEW_EVENT_RETENTION_DAYS + 1)
        )
        deleted = purge_old_view_events_task()
        self.assertEqual(deleted, 1)
        self.assertEqual(ProductViewEvent.objects.count(), 1)
        self.assertEqual(ProductViewEvent.objects.first().session_key, "recent-viewer")

    def test_purge_is_idempotent(self):
        """A rerun shortly after success deletes nothing further."""
        record_product_view(self.product, session_key="old-viewer")
        ProductViewEvent.objects.filter(session_key="old-viewer").update(
            created_at=timezone.now() - timedelta(days=VIEW_EVENT_RETENTION_DAYS + 1)
        )
        self.assertEqual(purge_old_view_events_task(), 1)
        self.assertEqual(purge_old_view_events_task(), 0)


class ProductViewEventModelTests(SocialProofTestCase):
    """Exercises the durable event model's basic invariants."""

    def setUp(self):
        super().setUp()
        _PRODUCT_SEQ[0] += 1
        self.product = _make_product(name="Fan", slug="fan")

    def test_deleting_product_cascades_events(self):
        """Deleting a product removes its view events."""
        record_product_view(self.product, session_key="viewer-a")
        self.product.delete()
        self.assertEqual(ProductViewEvent.objects.count(), 0)

    def test_events_ordered_newest_first(self):
        """View events list in reverse chronological order."""
        for index in range(3):
            record_product_view(self.product, session_key=f"viewer-{index}")
            time.sleep(0.01)
        pks = list(ProductViewEvent.objects.values_list("pk", flat=True))
        self.assertEqual(pks, sorted(pks, reverse=True))
