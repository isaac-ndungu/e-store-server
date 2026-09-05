"""Tests for the social proof app.

Covers the live-viewer counter (Redis semantics emulated on the test cache),
the durable view-event record, the feature-flag gate, both public API
endpoints, and the concurrent-views scenario the feature is designed for:
distinct sessions counting once each, repeated views by one session not
inflating the live count, and hidden/missing products resolving to 404.
"""

import time
from threading import Thread

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.catalog.models import Product
from apps.core.models import SiteConfig
from apps.social_proof import cache as proof_cache
from apps.social_proof.models import ProductViewEvent
from apps.social_proof.services import get_live_viewer_count, record_product_view

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

    def test_repeated_views_by_one_session_keep_one_live_viewer(self):
        """One session viewing repeatedly stays a single live viewer."""
        record_product_view(self.product, session_key="viewer-a")
        record_product_view(self.product, session_key="viewer-a")
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
    """Exercises the public view-recording endpoint."""

    def setUp(self):
        super().setUp()
        self.product = _make_product(name="Microwave", slug="microwave")
        self.record_url = reverse("api:social_proof:product-view", args=["microwave"])
        self.viewers_url = reverse(
            "api:social_proof:product-viewers", args=["microwave"]
        )

    def test_anonymous_can_record_a_view(self):
        """Recording a view is open to any caller."""
        response = self.client.post(self.record_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["live_viewers"], 1)

    def test_anonymous_can_read_viewer_count(self):
        """Reading the live count is open to any caller."""
        response = self.client.get(self.viewers_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["live_viewers"], 0)

    def test_post_increments_then_get_reads_count(self):
        """A recorded view is immediately visible to the reader."""
        self.client.post(self.record_url, {}, format="json")
        self.client.post(self.record_url, {}, format="json")
        response = self.client.get(self.viewers_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["live_viewers"], 1)

    def test_supplied_session_key_is_used(self):
        """A supplied session key identifies the viewing session."""
        self.client.post(
            self.record_url, {"session_key": "postman-viewer"}, format="json"
        )
        self.assertEqual(
            ProductViewEvent.objects.filter(session_key="postman-viewer").count(), 1
        )

    def test_simulated_concurrent_views_from_distinct_sessions(self):
        """Concurrency is simulated by distinct supplied session keys."""
        for index in range(12):
            response = self.client.post(
                self.record_url, {"session_key": f"concurrent-{index}"}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        read = self.client.get(self.viewers_url)
        self.assertEqual(read.data["live_viewers"], 12)
        self.assertEqual(
            ProductViewEvent.objects.filter(
                session_key__startswith="concurrent-"
            ).count(),
            12,
        )

    def test_repeated_supplied_key_keeps_one_live_viewer(self):
        """Re-arming the same supplied key does not inflate the count."""
        for _ in range(5):
            self.client.post(
                self.record_url, {"session_key": "repeat-viewer"}, format="json"
            )
        read = self.client.get(self.viewers_url)
        self.assertEqual(read.data["live_viewers"], 1)

    def test_blank_session_key_falls_back_to_session(self):
        """An empty supplied key behaves like no key at all."""
        response = self.client.post(
            self.record_url, {"session_key": "   "}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["live_viewers"], 1)

    def test_oversized_session_key_rejected(self):
        """A session key longer than the storage limit is a 400."""
        response = self.client.post(
            self.record_url, {"session_key": "x" * 101}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
