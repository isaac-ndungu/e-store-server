"""Tests for the bundles app.

Covers public browse endpoints (bundle list/detail/pricing with active-window
handling), the price-calculation service (exact decimal math, percent vs fixed
discounts, discount capping, price caching and invalidation), admin CRUD for
bundles and their component items, and the security / role separation between
anonymous, customer, and admin callers.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bundles.models import Bundle, BundleItem
from apps.catalog.models import Brand, Category, Product, ProductVariant

URLS = {
    "bundles": reverse("api:bundles:bundle-list"),
    "admin_bundles": reverse("api:bundles:admin-bundle-list-create"),
}


def _make_user(password="StrongPass123!", **kwargs):
    """Create a user with defaults appropriate for bundle tests."""
    return User.objects.create_user(
        email=kwargs.pop("email", "buyer@example.com"),
        username=kwargs.pop("username", "buyer"),
        password=password,
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


def _unique_slug(value):
    """Return a slug unique among bundles in the test DB.

    Args:
        value (str): the source text.

    Returns:
        str: a unique slug.
    """
    base = slugify(value)
    slug = base
    counter = 1
    while Bundle.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _make_product(name, slug, sku, **kwargs):
    """Create a product with a reusable category and brand."""
    category = (
        kwargs.pop("category", None)
        or Category.objects.get_or_create(name="Appliances", slug="appliances")[0]
    )
    brand = (
        kwargs.pop("brand", None)
        or Brand.objects.get_or_create(name="Samsung", slug="samsung")[0]
    )
    unique_slug = slug
    counter = 1
    while Product.objects.filter(slug=unique_slug).exists():
        unique_slug = f"{slug}-{counter}"
        counter += 1
    unique_sku = sku
    counter = 1
    while Product.objects.filter(sku=unique_sku).exists():
        unique_sku = f"{sku}-{counter}"
        counter += 1
    product = Product.objects.create(
        name=name,
        slug=unique_slug,
        sku=unique_sku,
        description="A test product.",
        category=category,
        brand=brand,
        is_active=True,
    )
    return product


def _make_variant(product, sku, price, **kwargs):
    """Create an active variant for a product with a unique SKU."""
    unique_sku = sku
    counter = 1
    while ProductVariant.objects.filter(sku=unique_sku).exists():
        unique_sku = f"{sku}-{counter}"
        counter += 1
    return ProductVariant.objects.create(
        product=product,
        sku=unique_sku,
        attributes=kwargs.pop("attributes", {"color": "Silver"}),
        price=price,
        is_active=True,
    )


def _make_bundle(name="Kitchen Starter", items=None, **kwargs):
    """Create a bundle, optionally with items, and return it."""
    product_a = _make_product("Kettle", "kettle", "KTL-1")
    product_b = _make_product("Toaster", "toaster", "TST-1")
    variant_a = _make_variant(product_a, "KTL-1-SILVER", "5000.00")
    variant_b = _make_variant(product_b, "TST-1-SILVER", "7000.00")

    bundle = Bundle.objects.create(
        name=name,
        slug=kwargs.pop("slug", None) or _unique_slug(name),
        discount_type=kwargs.pop("discount_type", "percent"),
        discount_value=kwargs.pop("discount_value", "0.00"),
        is_active=kwargs.pop("is_active", True),
    )
    if items is None:
        items = [
            {"product": product_a, "variant": variant_a, "quantity": 1},
            {"product": product_b, "variant": variant_b, "quantity": 1},
        ]
    for item in items:
        BundleItem.objects.create(bundle=bundle, **item)
    return bundle


class BundleServiceTests(APITestCase):
    """Exercises the price-calculation service directly."""

    def setUp(self):
        cache.clear()

    def _products(self):
        """Return two products with their single variant each."""
        product_a = _make_product("Kettle", "srv-kettle", "SRV-KTL")
        variant_a = _make_variant(product_a, "SRV-KTL-1", "5000.00")
        product_b = _make_product("Toaster", "srv-toaster", "SRV-TST")
        variant_b = _make_variant(product_b, "SRV-TST-1", "7000.00")
        return product_a, variant_a, product_b, variant_b

    def test_percent_discount_price(self):
        """A percent discount reduces the regular total proportionally."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, variant_b = self._products()
        bundle = Bundle.objects.create(
            name="Kitchen",
            slug="srv-kitchen",
            discount_type="percent",
            discount_value="10.00",
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_b.product, variant=variant_b
        )

        price = get_bundle_price(bundle)
        self.assertEqual(price["regular_price"], "12000.00")
        self.assertEqual(price["discount"], "1200.00")
        self.assertEqual(price["price"], "10800.00")

    def test_fixed_discount_price(self):
        """A fixed discount reduces the regular total by the amount."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, variant_b = self._products()
        bundle = Bundle.objects.create(
            name="Kitchen",
            slug="srv-kitchen",
            discount_type="fixed",
            discount_value="1500.00",
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_b.product, variant=variant_b
        )

        price = get_bundle_price(bundle)
        self.assertEqual(price["price"], "10500.00")

    def test_fixed_discount_capped_at_regular_total(self):
        """A fixed discount larger than the total never yields a negative price."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, _ = self._products()
        bundle = Bundle.objects.create(
            name="Single",
            slug="srv-single",
            discount_type="fixed",
            discount_value="99999.00",
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )

        price = get_bundle_price(bundle)
        self.assertEqual(price["price"], "0.00")

    def test_quantity_multiplies_line_total(self):
        """Line totals scale with each item's quantity."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, _ = self._products()
        bundle = Bundle.objects.create(
            name="Bulk", slug="srv-bulk", discount_type="fixed", discount_value="0.00"
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a, quantity=3
        )

        price = get_bundle_price(bundle)
        self.assertEqual(price["regular_price"], "15000.00")
        self.assertEqual(price["price"], "15000.00")

    def test_product_only_item_prices_lowest_active_variant(self):
        """A product-only item is priced at its cheapest active variant."""
        from apps.bundles.services import get_bundle_price

        product_a = _make_product("Kettle", "srv-kettle", "SRV-KTL")
        _make_variant(product_a, "SRV-KTL-1", "5000.00")
        _make_variant(product_a, "SRV-KTL-2", "4500.00")

        bundle = Bundle.objects.create(
            name="Kettle",
            slug="srv-kettle-bundle",
            discount_type="fixed",
            discount_value="0.00",
        )
        BundleItem.objects.create(bundle=bundle, product=product_a)

        price = get_bundle_price(bundle)
        self.assertEqual(price["regular_price"], "4500.00")

    def test_price_is_cached_and_read_from_cache(self):
        """A second call reads the cached value without recomputation."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, variant_b = self._products()
        bundle = Bundle.objects.create(
            name="Kitchen",
            slug="srv-kitchen-cache",
            discount_type="percent",
            discount_value="10.00",
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_b.product, variant=variant_b
        )

        with CaptureQueriesContext(connection) as ctx:
            get_bundle_price(bundle)
            cold_count = len(ctx.captured_queries)
        with CaptureQueriesContext(connection) as ctx:
            get_bundle_price(bundle)
            warm_count = len(ctx.captured_queries)
        self.assertLess(warm_count, cold_count)

    def test_price_cache_invalidated_on_bundle_save(self):
        """Updating a bundle invalidates its cached price."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, variant_b = self._products()
        bundle = Bundle.objects.create(
            name="Kitchen",
            slug="srv-kitchen-inv",
            discount_type="fixed",
            discount_value="0.00",
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_b.product, variant=variant_b
        )

        first = get_bundle_price(bundle)
        bundle.discount_value = Decimal("2000.00")
        bundle.save()
        bundle.refresh_from_db()
        second = get_bundle_price(bundle)
        self.assertEqual(second["discount"], "2000.00")
        self.assertNotEqual(first["price"], second["price"])

    def test_price_cache_invalidated_on_item_change(self):
        """Changing a bundle item invalidates the cached price."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, variant_b = self._products()
        bundle = Bundle.objects.create(
            name="Kitchen",
            slug="srv-kitchen-item",
            discount_type="fixed",
            discount_value="0.00",
        )
        item_a = BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_b.product, variant=variant_b
        )

        first = get_bundle_price(bundle)
        item_a.quantity = 2
        item_a.save()
        second = get_bundle_price(bundle)
        self.assertEqual(second["regular_price"], "17000.00")
        self.assertNotEqual(first["regular_price"], second["regular_price"])

    def test_price_cache_invalidated_on_variant_price_change(self):
        """Editing a component variant's price invalidates the bundle cache."""
        from apps.bundles.services import get_bundle_price

        _, variant_a, _, variant_b = self._products()
        bundle = Bundle.objects.create(
            name="Kitchen",
            slug="srv-kitchen-variant",
            discount_type="fixed",
            discount_value="0.00",
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_a.product, variant=variant_a
        )
        BundleItem.objects.create(
            bundle=bundle, product=variant_b.product, variant=variant_b
        )

        first = get_bundle_price(bundle)
        self.assertEqual(first["regular_price"], "12000.00")
        variant_a.price = Decimal("6000.00")
        variant_a.save()
        second = get_bundle_price(bundle)
        self.assertEqual(second["regular_price"], "13000.00")
        self.assertNotEqual(first["regular_price"], second["regular_price"])


class BundleBrowseTests(APITestCase):
    """Exercises the public bundle list/detail/price endpoints."""

    def setUp(self):
        cache.clear()

    def test_bundle_list_public_and_active_only(self):
        """Only active bundles appear in the public list."""
        _make_bundle(name="Active Bundle", slug="active-bundle-list")
        Bundle.objects.create(
            name="Hidden",
            slug="hidden-bundle-list",
            discount_type="fixed",
            discount_value="0.00",
            is_active=False,
        )
        response = self.client.get(URLS["bundles"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "active-bundle-list")

    def test_bundle_detail_returns_items(self):
        """A bundle detail includes its component items."""
        bundle = _make_bundle(name="Kitchen", slug="detail-kitchen")
        url = reverse("api:bundles:bundle-detail", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["items"]), 2)

    def test_inactive_bundle_detail_returns_404(self):
        """An inactive bundle is not retrievable publicly."""
        bundle = Bundle.objects.create(
            name="Hidden",
            slug="hidden-detail",
            discount_type="fixed",
            discount_value="0.00",
            is_active=False,
        )
        url = reverse("api:bundles:bundle-detail", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_out_of_window_bundle_detail_returns_404(self):
        """A bundle outside its window is not retrievable publicly."""
        bundle = Bundle.objects.create(
            name="Past",
            slug="past-window",
            discount_type="fixed",
            discount_value="0.00",
            is_active=True,
            starts_at=timezone.now() - timedelta(days=3),
            ends_at=timezone.now() - timedelta(days=1),
        )
        url = reverse("api:bundles:bundle-detail", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_missing_bundle_detail_returns_404(self):
        """A nonexistent bundle slug returns 404."""
        url = reverse("api:bundles:bundle-detail", args=["no-such-bundle"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bundle_price_returns_breakdown(self):
        """The price endpoint returns the computed breakdown."""
        bundle = _make_bundle(
            name="Kitchen Price",
            slug="kitchen-price",
            discount_type="percent",
            discount_value="10.00",
        )
        url = reverse("api:bundles:bundle-price", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["regular_price"], "12000.00")
        self.assertEqual(response.data["discount"], "1200.00")
        self.assertEqual(response.data["price"], "10800.00")
        self.assertEqual(len(response.data["items"]), 2)

    def test_bundle_price_404_for_inactive(self):
        """The price endpoint hides inactive bundles."""
        bundle = Bundle.objects.create(
            name="Hidden",
            slug="hidden-price",
            discount_type="fixed",
            discount_value="0.00",
            is_active=False,
        )
        url = reverse("api:bundles:bundle-price", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bundle_price_404_for_unpriceable(self):
        """An active but unpriced bundle is not offerable to the storefront."""
        bundle = Bundle.objects.create(
            name="Empty",
            slug="empty-price",
            discount_type="fixed",
            discount_value="0.00",
            is_active=True,
        )
        url = reverse("api:bundles:bundle-price", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bundles_list_empty_when_feature_disabled(self):
        """The storefront hides bundles when the feature flag is off."""
        _make_bundle(name="Active Bundle", slug="flag-off-list")
        from apps.core.models import SiteConfig

        config = SiteConfig.load()
        config.settings = {**config.settings, "enable_bundles": False}
        config.save()
        response = self.client.get(URLS["bundles"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_bundle_detail_404_when_feature_disabled(self):
        """Bundle detail is hidden when the feature flag is off."""
        bundle = _make_bundle(name="Active Bundle", slug="flag-off-detail")
        from apps.core.models import SiteConfig

        config = SiteConfig.load()
        config.settings = {**config.settings, "enable_bundles": False}
        config.save()
        url = reverse("api:bundles:bundle-detail", args=[bundle.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class BundleAdminTests(APITestCase):
    """Exercises admin bundle and item CRUD and permissions."""

    def setUp(self):
        cache.clear()
        _make_admin()

    def test_anonymous_cannot_manage_bundles(self):
        """An unauthenticated caller cannot manage bundles."""
        response = self.client.get(URLS["admin_bundles"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(
            URLS["admin_bundles"],
            {"name": "B", "discount_type": "fixed", "discount_value": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_bundles(self):
        """A plain customer token is rejected from bundle creation."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_bundles"],
            {"name": "B", "discount_type": "fixed", "discount_value": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Bundle.objects.count(), 0)

    def test_admin_can_create_bundle_with_autoslug(self):
        """An admin can create a bundle and its slug is auto-generated."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_bundles"],
            {
                "name": "Kitchen Starter",
                "discount_type": "percent",
                "discount_value": "10.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], "kitchen-starter")

    def test_invalid_discount_type_rejected(self):
        """An unknown discount type is rejected with a 400."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_bundles"],
            {"name": "Bad", "discount_type": "half", "discount_value": "10.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_can_update_bundle(self):
        """An admin can update a bundle."""
        _login(self.client)
        bundle = _make_bundle(name="Old", slug="admin-old")
        url = reverse("api:bundles:admin-bundle-detail", args=[bundle.pk])
        response = self.client.patch(url, {"discount_value": "20.00"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        bundle.refresh_from_db()
        self.assertEqual(str(bundle.discount_value), "20.00")

    def test_admin_can_delete_bundle(self):
        """An admin can delete a bundle."""
        _login(self.client)
        bundle = _make_bundle(name="Delete Me", slug="admin-delete")
        url = reverse("api:bundles:admin-bundle-detail", args=[bundle.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Bundle.objects.count(), 0)

    def test_admin_can_add_item_to_bundle(self):
        """An admin can add a component item to a bundle."""
        _login(self.client)
        bundle = _make_bundle(name="Grow", slug="admin-grow")
        product = _make_product("Blender", "blender", "BLD-1")
        variant = _make_variant(product, "BLD-1-SILVER", "8000.00")
        url = reverse("api:bundles:admin-bundle-item-list-create", args=[bundle.pk])
        response = self.client.post(
            url,
            {"product": product.id, "variant": variant.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(bundle.items.count(), 3)

    def test_admin_item_rejects_variant_from_other_product(self):
        """A bundle item cannot pair a variant with a different product."""
        _login(self.client)
        bundle = _make_bundle(name="Strict", slug="admin-strict")
        product_a = _make_product("Kettle B", "kettle-b", "KTL-B")
        product_b = _make_product("Toaster B", "toaster-b", "TST-B")
        variant_b = _make_variant(product_b, "TST-B-1", "7000.00")
        url = reverse("api:bundles:admin-bundle-item-list-create", args=[bundle.pk])
        response = self.client.post(
            url,
            {"product": product_a.pk, "variant": variant_b.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(bundle.items.count(), 2)

    def test_admin_item_scoped_to_parent_bundle(self):
        """An item can only be addressed through its own bundle."""
        _login(self.client)
        bundle_a = _make_bundle(name="Bundle A", slug="admin-scope-a")
        bundle_b = _make_bundle(name="Bundle B", slug="admin-scope-b")
        item = bundle_a.items.first()
        url = reverse(
            "api:bundles:admin-bundle-item-detail", args=[bundle_b.pk, item.pk]
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_item_patch_cannot_repoint_bundle(self):
        """A PATCH on an item cannot move it to a different bundle."""
        _login(self.client)
        bundle_a = _make_bundle(name="Owner", slug="admin-owner")
        bundle_b = _make_bundle(name="Other", slug="admin-other")
        item = bundle_a.items.first()
        url = reverse(
            "api:bundles:admin-bundle-item-detail", args=[bundle_a.pk, item.pk]
        )
        response = self.client.patch(url, {"bundle": bundle_b.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.bundle_id, bundle_a.pk)
