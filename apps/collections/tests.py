"""Tests for the collections app.

Covers the smart-collection membership computation (new arrivals, recently
restocked, almost-gone / low stock, plus the not-yet-computable rules), the
refresh flow that rewrites membership rows and caches the product list, the
cache invalidation signals (collection rename, membership changes), the
public collection endpoints (list filtered by type/location, detail with
products, query-count bounds), the admin CRUD endpoints (type/rule
invariants, duplicate-membership rejection, database constraints), and the
security separation between anonymous, customer, and admin callers.
"""

from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Product, ProductVariant
from apps.collections import cache as collection_cache
from apps.collections.models import Collection, CollectionMembership
from apps.collections.services import (
    compute_membership,
    refresh_all_smart_collections,
    refresh_smart_collection,
)
from apps.inventory.models import Warehouse
from apps.inventory.services import receive_stock

URLS = {
    "collections": reverse("api:collections:collection-list"),
    "admin_collections": reverse("api:collections:admin-collection-list-create"),
    "admin_memberships": reverse(
        "api:collections:admin-collection-membership-list-create"
    ),
}

_PRODUCT_SEQ = [0]

_DEFAULT_DESCRIPTION = "A test appliance."


def _make_user(email="buyer@example.com", username="buyer", **kwargs):
    """Create a plain customer user for permission tests."""
    return User.objects.create_user(
        email=email,
        username=username,
        password=kwargs.pop("password", "StrongPass123!"),
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        **kwargs,
    )


def _make_admin():
    """Create and return a staff superuser for admin CRUD tests."""
    return User.objects.create_user(
        email="manager@example.com",
        username="manager",
        password="ManagerPass123!",
        phone_number="+254700000000",
        is_staff=True,
        is_superuser=True,
    )


def _login(client, email="manager@example.com", password="ManagerPass123!"):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_product(**kwargs):
    """Create a product with unique slugs and skus, optionally with a variant."""
    _PRODUCT_SEQ[0] += 1
    n = _PRODUCT_SEQ[0]
    product = Product.objects.create(
        name=kwargs.pop("name", f"Appliance {n}"),
        slug=kwargs.pop("slug", f"appliance-{n}"),
        sku=kwargs.pop("sku", f"APP-{n}"),
        description=kwargs.pop("description", _DEFAULT_DESCRIPTION),
        **kwargs,
    )
    variant = None
    if kwargs.pop("with_variant", True):
        variant = ProductVariant.objects.create(
            product=product,
            sku=kwargs.pop("variant_sku", f"APP-{n}-V1"),
            price=kwargs.pop("price", "25000.00"),
        )
    return (product, variant) if variant is not None else product


def _make_collection(**kwargs):
    """Create a collection with sensible defaults."""
    return Collection.objects.create(
        name=kwargs.pop("name", "Home Appliances"),
        slug=kwargs.pop("slug", "home-appliances"),
        collection_type=kwargs.pop("collection_type", "manual"),
        smart_rule=kwargs.pop("smart_rule", ""),
        rule_window_days=kwargs.pop("rule_window_days", 14),
        rule_threshold=kwargs.pop("rule_threshold", None),
        description=kwargs.pop("description", "A test collection."),
        display_location=kwargs.pop("display_location", ""),
        sort_order=kwargs.pop("sort_order", 0),
        is_active=kwargs.pop("is_active", True),
        starts_at=kwargs.pop("starts_at", None),
        ends_at=kwargs.pop("ends_at", None),
    )


class CollectionsAPITestCase(APITestCase):
    """Base case that resets the shared test cache for throttle budgets."""

    def setUp(self):
        """Clear throttling and collection caches so each test starts fresh."""
        cache.clear()
        super().setUp()


class SmartMembershipServiceTests(CollectionsAPITestCase):
    """Exercises the smart-rule membership computation."""

    def test_new_arrivals_match_products_created_in_window(self):
        """Only products created within the rule window are returned."""
        recent, _ = _make_product(name="Recent", slug="recent")
        Product.objects.filter(pk=recent.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        old, _ = _make_product(name="Old", slug="old")
        Product.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        collection = _make_collection(
            collection_type="smart", smart_rule="new_arrivals", rule_window_days=14
        )
        pks = compute_membership(collection)
        self.assertIn(recent.pk, pks)
        self.assertNotIn(old.pk, pks)

    def test_new_arrivals_exclude_inactive_products(self):
        """Discontinued or inactive products never match new arrivals."""
        product, _ = _make_product(name="Hidden", slug="hidden", is_active=False)
        Product.objects.filter(pk=product.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        collection = _make_collection(
            collection_type="smart", smart_rule="new_arrivals", rule_window_days=14
        )
        self.assertEqual(compute_membership(collection), [])

    def test_restocked_match_restocked_in_window(self):
        """Products restocked within the window match, oldest excluded."""
        fresh_restock, _ = _make_product(name="Fresh", slug="fresh")
        Product.objects.filter(pk=fresh_restock.pk).update(
            last_restocked_at=timezone.now() - timedelta(days=2)
        )
        stale_restock, _ = _make_product(name="Stale", slug="stale")
        Product.objects.filter(pk=stale_restock.pk).update(
            last_restocked_at=timezone.now() - timedelta(days=60)
        )
        never, _ = _make_product(name="Never", slug="never", last_restocked_at=None)
        collection = _make_collection(
            collection_type="smart", smart_rule="restocked", rule_window_days=14
        )
        pks = compute_membership(collection)
        self.assertIn(fresh_restock.pk, pks)
        self.assertNotIn(stale_restock.pk, pks)
        self.assertNotIn(never.pk, pks)

    def test_low_stock_matches_variants_at_or_below_threshold(self):
        """A variant with available stock at/below the threshold matches."""
        low, low_var = _make_product(name="Low", slug="low")
        healthy, healthy_var = _make_product(name="Healthy", slug="healthy")
        warehouse = Warehouse.objects.create(name="Main")
        receive_stock(variant=low_var, warehouse=warehouse, quantity=2)
        receive_stock(variant=healthy_var, warehouse=warehouse, quantity=50)
        collection = _make_collection(
            collection_type="smart",
            smart_rule="low_stock",
            rule_threshold=5,
        )
        pks = compute_membership(collection)
        self.assertIn(low.pk, pks)
        self.assertNotIn(healthy.pk, pks)

    def test_low_stock_aggregates_available_across_warehouses(self):
        """Available stock is summed over all warehouses for the threshold."""
        product, variant = _make_product(name="Split", slug="split")
        warehouse_a = Warehouse.objects.create(name="A")
        warehouse_b = Warehouse.objects.create(name="B")
        receive_stock(variant=variant, warehouse=warehouse_a, quantity=3)
        receive_stock(variant=variant, warehouse=warehouse_b, quantity=3)
        collection = _make_collection(
            collection_type="smart", smart_rule="low_stock", rule_threshold=5
        )
        # 3 + 3 = 6 available, above the threshold -> not low.
        self.assertNotIn(product.pk, compute_membership(collection))

    def test_low_stock_excludes_stock_trapped_in_inactive_warehouse(self):
        """Stock in a decommissioned warehouse is not sellable and so a
        product whose only stock sits there is not ``almost gone`` — it has
        nothing left to almost run out of."""
        product, variant = _make_product(name="Mothballed", slug="mothballed")
        closed = Warehouse.objects.create(name="Closed")
        receive_stock(variant=variant, warehouse=closed, quantity=2)
        closed.is_active = False
        closed.save()
        collection = _make_collection(
            collection_type="smart", smart_rule="low_stock", rule_threshold=5
        )
        self.assertNotIn(product.pk, compute_membership(collection))

    def test_unavailable_and_pending_rules_return_empty(self):
        """Rules whose data sources are pending compute to an empty set."""
        for index, rule in enumerate(("on_sale", "best_sellers")):
            collection = _make_collection(
                name=f"Pending {rule}",
                slug=f"pending-{rule}-{index}",
                collection_type="smart",
                smart_rule=rule,
            )
            self.assertEqual(compute_membership(collection), [])

    def test_compute_raises_for_non_smart_collection(self):
        """Manual collections have no computable membership."""
        manual = _make_collection()
        with self.assertRaisesRegex(ValueError, "smart collection"):
            compute_membership(manual)


class SmartRefreshServiceTests(CollectionsAPITestCase):
    """Exercises the smart-collection refresh flow."""

    def test_refresh_rewrites_membership_rows(self):
        """A refresh replaces the previous membership with the fresh result."""
        product, _ = _make_product(name="New", slug="new")
        Product.objects.filter(pk=product.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        collection = _make_collection(
            collection_type="smart", smart_rule="new_arrivals"
        )
        CollectionMembership.objects.create(
            collection=collection, product=product, sort_order=0
        )
        self.assertEqual(collection.memberships.count(), 1)
        self.assertTrue(refresh_smart_collection(collection))
        self.assertEqual(collection.memberships.count(), 1)

    def test_refresh_skips_manual_collections(self):
        """Manual collections are never touched by the refresh."""
        collection = _make_collection(collection_type="manual")
        self.assertFalse(refresh_smart_collection(collection))

    def test_refresh_skips_collection_outside_window(self):
        """A collection outside its window is not refreshed."""
        collection = _make_collection(
            collection_type="smart",
            smart_rule="new_arrivals",
            starts_at=timezone.now() + timedelta(days=3),
        )
        self.assertFalse(refresh_smart_collection(collection))

    def test_refresh_caches_product_list_per_slug(self):
        """The refreshed product id list is cached for the storefront read."""
        product, _ = _make_product(name="Cached", slug="cached")
        Product.objects.filter(pk=product.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        collection = _make_collection(
            collection_type="smart", smart_rule="new_arrivals"
        )
        refresh_smart_collection(collection)
        cached = collection_cache.get_cached_product_pks(collection.slug)
        self.assertEqual(cached, [product.pk])

    def test_refresh_all_recomputes_every_smart_collection(self):
        """The bulk refresh updates all in-window smart collections."""
        recent, _ = _make_product(name="Bulk", slug="bulk")
        Product.objects.filter(pk=recent.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        smart = _make_collection(
            name="Smart",
            slug="smart",
            collection_type="smart",
            smart_rule="new_arrivals",
        )
        _make_collection(name="Manual", slug="manual", collection_type="manual")
        refreshed = refresh_all_smart_collections()
        self.assertEqual(refreshed, 1)
        self.assertEqual(smart.memberships.count(), 1)

    def test_refresh_sets_last_refreshed_at(self):
        """A successful refresh records when the membership was rebuilt."""
        product, _ = _make_product(name="Timed", slug="timed")
        Product.objects.filter(pk=product.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        collection = _make_collection(
            collection_type="smart", smart_rule="new_arrivals"
        )
        refresh_smart_collection(collection)
        refreshed = Collection.objects.get(pk=collection.pk)
        self.assertIsNotNone(refreshed.last_refreshed_at)

    def test_refresh_is_idempotent(self):
        """A repeated refresh converges on the same membership and cache."""
        product, _ = _make_product(name="Repeat", slug="repeat")
        Product.objects.filter(pk=product.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        collection = _make_collection(
            collection_type="smart", smart_rule="new_arrivals"
        )
        refresh_smart_collection(collection)
        first_pks = collection_cache.get_cached_product_pks(collection.slug)
        refresh_smart_collection(collection)
        self.assertEqual(
            collection_cache.get_cached_product_pks(collection.slug), first_pks
        )
        self.assertEqual(collection.memberships.count(), 1)


class CacheInvalidationSignalTests(CollectionsAPITestCase):
    """Exercises the signal-based cache invalidation."""

    def _warm(self, collection, product):
        """Cache a product list for a collection."""
        collection_cache.cache_product_pks(collection.slug, [product.pk])

    def test_renaming_collection_invalidates_old_and_new_slug(self):
        """A rename purges the old key so no stale list is served."""
        collection = _make_collection(slug="old-slug")
        product, _ = _make_product(name="P1", slug="p1")
        self._warm(collection, product)
        self.assertIsNotNone(collection_cache.get_cached_product_pks("old-slug"))
        collection.slug = "new-slug"
        collection.save()
        self.assertIsNone(collection_cache.get_cached_product_pks("old-slug"))
        self.assertIsNone(collection_cache.get_cached_product_pks("new-slug"))

    def test_deleting_collection_invalidates_cache(self):
        """Deleting a collection drops its cached list."""
        collection = _make_collection(slug="del-me")
        product, _ = _make_product(name="Gone", slug="gone")
        self._warm(collection, product)
        collection.delete()
        self.assertIsNone(collection_cache.get_cached_product_pks("del-me"))

    def test_membership_write_invalidates_parent_cache(self):
        """Creating a membership row drops the parent's cached list."""
        collection = _make_collection(slug="mem-write")
        product, _ = _make_product(name="Mem", slug="mem")
        self._warm(collection, product)
        CollectionMembership.objects.create(collection=collection, product=product)
        self.assertIsNone(collection_cache.get_cached_product_pks("mem-write"))


class CollectionEndpointTests(CollectionsAPITestCase):
    """Exercises the public collection endpoints."""

    def test_collections_are_public(self):
        """An anonymous caller can list active collections."""
        _make_collection()
        response = self.client.get(URLS["collections"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_inactive_collections_excluded(self):
        """Inactive collections do not appear in the public list."""
        _make_collection(name="Active", slug="active")
        _make_collection(name="Hidden", slug="hidden", is_active=False)
        response = self.client.get(URLS["collections"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["name"], "Active")

    def test_out_of_window_collections_excluded_from_public_list(self):
        """A collection outside its start/end window is not offered publicly."""
        _make_collection(
            name="Future", slug="future", starts_at=timezone.now() + timedelta(days=2)
        )
        _make_collection(
            name="Past", slug="past", ends_at=timezone.now() - timedelta(days=2)
        )
        _make_collection(name="Live", slug="live")
        response = self.client.get(URLS["collections"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["name"], "Live")

    def test_out_of_window_collection_detail_is_404(self):
        """A collection outside its window is not retrievable publicly."""
        _make_collection(
            name="Future", slug="future", starts_at=timezone.now() + timedelta(days=2)
        )
        detail_url = reverse("api:collections:collection-detail", args=["future"])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_collection_type_filter_narrows_list(self):
        """A collection_type query filter narrows the public list."""
        _make_collection(name="Manual", slug="manual", collection_type="manual")
        _make_collection(
            name="Smart",
            slug="smart",
            collection_type="smart",
            smart_rule="new_arrivals",
        )
        response = self.client.get(URLS["collections"], {"collection_type": "smart"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["smart_rule"], "new_arrivals")

    def test_display_location_filter_narrows_list(self):
        """A display_location query filter narrows the public list."""
        _make_collection(name="Home", slug="home", display_location="homepage")
        _make_collection(name="Shop", slug="shop", display_location="shop")
        response = self.client.get(
            URLS["collections"], {"display_location": "homepage"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["name"], "Home")

    def test_collection_list_uses_fixed_query_count(self):
        """Listing collections renders without a query per row."""
        for index in range(4):
            _make_collection(name=f"Collection {index}", slug=f"collection-{index}")
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(URLS["collections"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 4)
        self.assertLess(len(captured), 8)

    def test_collection_detail_returns_products(self):
        """The detail endpoint renders the collection with its products."""
        collection = _make_collection(slug="detail")
        product, _ = _make_product(name="Fridge", slug="fridge")
        CollectionMembership.objects.create(collection=collection, product=product)
        detail_url = reverse("api:collections:collection-detail", args=["detail"])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Home Appliances")
        self.assertEqual(len(response.data["products"]), 1)
        self.assertEqual(response.data["products"][0]["slug"], "fridge")

    def test_collection_detail_is_found_by_slug(self):
        """The detail resolves by slug, not by id."""
        _make_collection(name="Home", slug="home")
        detail_url = reverse("api:collections:collection-detail", args=["home"])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Home")

    def test_collection_detail_rejects_unknown_slug(self):
        """An unknown slug is a 404."""
        detail_url = reverse("api:collections:collection-detail", args=["unknown"])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_collection_detail_rejects_inactive_collection(self):
        """An inactive collection is not retrievable on the public endpoint."""
        _make_collection(slug="hidden", is_active=False)
        detail_url = reverse("api:collections:collection-detail", args=["hidden"])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_collection_detail_uses_cache_when_warm(self):
        """A warm cache answers the product list without extra queries."""
        collection = _make_collection(slug="cached")
        product, _ = _make_product(name="Cached", slug="cached-product")
        collection_cache.cache_product_pks(collection.slug, [product.pk])
        detail_url = reverse("api:collections:collection-detail", args=["cached"])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["products"]), 1)
        self.assertLess(len(captured), 8)


class AdminCollectionAPITests(CollectionsAPITestCase):
    """Exercises the admin-only collection endpoints."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def test_anonymous_cannot_manage_collections(self):
        """An unauthenticated caller is rejected from collection management."""
        response = self.client.get(URLS["admin_collections"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(
            URLS["admin_collections"],
            {"name": "Home", "slug": "home", "collection_type": "manual"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_collections(self):
        """A plain customer token cannot create a collection."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_collections"],
            {"name": "Home", "slug": "home", "collection_type": "manual"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Collection.objects.count(), 0)

    def test_admin_can_create_and_list_collections(self):
        """An admin can create a collection and see it in the list."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_collections"],
            {
                "name": "Home",
                "slug": "home",
                "collection_type": "manual",
                "description": "Ones to love.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Home")
        response = self.client.get(URLS["admin_collections"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_admin_can_update_and_delete_collection(self):
        """An admin can edit and delete a collection."""
        _login(self.client)
        collection = _make_collection(name="Old", slug="old")
        detail_url = reverse(
            "api:collections:admin-collection-detail", args=[collection.pk]
        )
        response = self.client.patch(detail_url, {"name": "New"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "New")
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Collection.objects.count(), 0)

    def test_admin_smart_collection_requires_smart_rule(self):
        """A smart collection without a rule is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_collections"],
            {"name": "Smart", "slug": "smart", "collection_type": "smart"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_manual_collection_cannot_carry_rule(self):
        """A manual collection with a rule is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_collections"],
            {
                "name": "Manual",
                "slug": "manual",
                "collection_type": "manual",
                "smart_rule": "new_arrivals",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_blank_name_rejected(self):
        """A whitespace-only name is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_collections"],
            {"name": "   ", "slug": "blank", "collection_type": "manual"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_collection_slug_unique_in_database(self):
        """The database enforces one collection per slug."""
        _make_collection(slug="dup")
        with self.assertRaises(IntegrityError):
            Collection.objects.create(name="Dup", slug="dup")


class AdminMembershipAPITests(CollectionsAPITestCase):
    """Exercises the admin-only membership endpoints."""

    def setUp(self):
        """Create the admin account and a collection used by the tests."""
        super().setUp()
        _make_admin()
        self.collection = _make_collection(slug="members")

    def _membership_payload(self):
        """Return a membership payload referencing a fresh product."""
        product, _ = _make_product(name="Member", slug="member")
        return {
            "collection": self.collection.pk,
            "product": product.pk,
            "sort_order": 0,
        }

    def test_anonymous_cannot_manage_memberships(self):
        """An unauthenticated caller is rejected from membership management."""
        response = self.client.get(URLS["admin_memberships"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_memberships(self):
        """A plain customer token cannot create a membership row."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_memberships"], self._membership_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(CollectionMembership.objects.count(), 0)

    def test_admin_can_create_and_list_memberships(self):
        """An admin can add a product to a collection and read it back."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_memberships"], self._membership_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["product_name"], "Member")
        response = self.client.get(
            URLS["admin_memberships"], {"collection": self.collection.pk}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_admin_can_update_and_delete_membership(self):
        """An admin can re-order and remove a membership row."""
        _login(self.client)
        product, _ = _make_product(name="Member", slug="member")
        membership = CollectionMembership.objects.create(
            collection=self.collection, product=product, sort_order=0
        )
        detail_url = reverse(
            "api:collections:admin-collection-membership-detail", args=[membership.pk]
        )
        response = self.client.patch(detail_url, {"sort_order": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sort_order"], 5)
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(CollectionMembership.objects.count(), 0)

    def test_duplicate_membership_is_rejected(self):
        """A product already in a collection cannot be added twice."""
        _login(self.client)
        product, _ = _make_product(name="Member", slug="member")
        payload = {
            "collection": self.collection.pk,
            "product": product.pk,
            "sort_order": 0,
        }
        response = self.client.post(URLS["admin_memberships"], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(URLS["admin_memberships"], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(CollectionMembership.objects.count(), 1)

    def test_membership_db_constraint_rejects_duplicate(self):
        """The database independently rejects a duplicate membership."""
        product, _ = _make_product(name="Member", slug="member")
        CollectionMembership.objects.create(collection=self.collection, product=product)
        with self.assertRaises(IntegrityError):
            CollectionMembership.objects.create(
                collection=self.collection, product=product
            )
