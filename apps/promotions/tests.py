
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bundles.models import Bundle, BundleItem
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.collections.models import Collection
from apps.collections.services import compute_membership
from apps.promotions.models import Coupon, CouponRedemption, Discount
from apps.promotions.services import (
    create_discount,
    get_effective_price,
    record_redemption,
    validate_coupon,
)

URLS = {
    "admin_discounts": reverse("api:promotions:admin-discount-list-create"),
    "admin_coupons": reverse("api:promotions:admin-coupon-list-create"),
    "coupon_validate": reverse("api:promotions:coupon-validate"),
}

_SEQ = [0]


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


def _make_product(name="Kettle", slug="kettle", sku="KTL-1", price="5000.00", **kwargs):
    """Create a product with a default active variant, ensuring unique keys."""
    _SEQ[0] += 1
    n = _SEQ[0]
    category = (
        kwargs.pop("category", None)
        or Category.objects.get_or_create(name="Appliances", slug="appliances")[0]
    )
    brand = (
        kwargs.pop("brand", None)
        or Brand.objects.get_or_create(name="Samsung", slug="samsung")[0]
    )
    unique_slug, unique_sku = f"{slug}-{n}", f"{sku}-{n}"
    product = Product.objects.create(
        name=f"{name} {n}",
        slug=unique_slug,
        sku=unique_sku,
        description="A test product.",
        category=category,
        brand=brand,
        is_active=True,
    )
    variant = ProductVariant.objects.create(
        product=product,
        sku=f"{unique_sku}-V",
        attributes={"color": "Silver"},
        price=price,
        is_active=True,
    )
    return product, variant


def _make_bundle(variant_a, variant_b, discount_type="percent", discount_value="0.00"):
    """Create a bundle of two variants with an optional discount."""
    bundle = Bundle.objects.create(
        name=f"Bundle {_SEQ[0]}",
        slug=f"bundle-{_SEQ[0]}-{_SEQ[0]}",
        discount_type=discount_type,
        discount_value=discount_value,
        is_active=True,
    )
    BundleItem.objects.create(
        bundle=bundle, product=variant_a.product, variant=variant_a
    )
    BundleItem.objects.create(
        bundle=bundle, product=variant_b.product, variant=variant_b
    )
    return bundle


def _make_coupon(code="SAVE10", discount_type="percent", value="10.00", **kwargs):
    """Create a coupon with default window starting in the past."""
    return Coupon.objects.create(
        code=code,
        discount_type=discount_type,
        value=value,
        starts_at=kwargs.pop("starts_at", timezone.now() - timedelta(days=1)),
        ends_at=kwargs.pop("ends_at", timezone.now() + timedelta(days=10)),
        is_active=kwargs.pop("is_active", True),
        **kwargs,
    )


class EffectivePriceServiceTests(APITestCase):
    """Exercises discount math, scope matching, and selection directly."""

    def setUp(self):
        cache.clear()

    def test_percent_discount(self):
        """A percent discount reduces the base price proportionally."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Spring Sale",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant.pk],
        )
        data = get_effective_price(variant)
        self.assertEqual(data["base_price"], "5000.00")
        self.assertEqual(data["price"], "4500.00")
        self.assertEqual(data["discount"], "500.00")
        self.assertEqual(data["discount_name"], "Spring Sale")

    def test_fixed_discount_capped_at_zero(self):
        """A fixed discount larger than the price never yields a negative price."""
        _, variant = _make_product(price="2000.00")
        create_discount(
            name="Flat",
            scope="variant",
            discount_type="fixed",
            value="9999.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant.pk],
        )
        data = get_effective_price(variant)
        self.assertEqual(data["price"], "0.00")
        self.assertEqual(data["discount"], "2000.00")

    def test_higher_priority_wins(self):
        """The highest-priority discount is selected even when a lower one is cheaper."""
        _, variant = _make_product(price="5000.00")
        product = variant.product
        create_discount(
            name="Small",
            scope="product",
            discount_type="percent",
            value="30.00",
            starts_at=timezone.now() - timedelta(days=1),
            priority=20,
            products=[product.pk],
        )
        create_discount(
            name="Big",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            priority=10,
        )
        data = get_effective_price(variant)
        self.assertEqual(data["discount_name"], "Small")
        self.assertEqual(data["price"], "3500.00")

    def test_equal_priority_chooses_lowest_price(self):
        """Ties in priority go to the discount yielding the lowest price."""
        _, variant = _make_product(price="5000.00")
        product = variant.product
        create_discount(
            name="Percent",
            scope="product",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            products=[product.pk],
        )
        create_discount(
            name="Fixed",
            scope="sitewide",
            discount_type="fixed",
            value="600.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        data = get_effective_price(variant)
        self.assertEqual(data["discount_name"], "Fixed")
        self.assertEqual(data["price"], "4400.00")

    def test_sitewide_scope_applies_to_every_variant(self):
        """A sitewide discount reduces any variant's price."""
        _, variant = _make_product(price="10000.00")
        create_discount(
            name="Sitewide",
            scope="sitewide",
            discount_type="percent",
            value="5.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(get_effective_price(variant)["price"], "9500.00")

    def test_category_and_brand_scopes(self):
        """Category and brand discounts match products through their relations."""
        _, variant = _make_product(price="10000.00")
        create_discount(
            name="Category",
            scope="category",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            categories=[variant.product.category_id],
        )
        data = get_effective_price(variant)
        self.assertEqual(data["price"], "9000.00")

        create_discount(
            name="Brand",
            scope="brand",
            discount_type="percent",
            value="5.00",
            starts_at=timezone.now() - timedelta(days=1),
            priority=5,
            brands=[variant.product.brand_id],
        )
        # Only the best single discount applies; the brand discount wins by
        # priority, so 5% off 10000 leaves 9500.
        data = get_effective_price(variant)
        self.assertEqual(data["price"], "9500.00")

    def test_no_discount_returns_base_price(self):
        """Without a discount the effective price equals the base price."""
        _, variant = _make_product(price="3000.00")
        data = get_effective_price(variant)
        self.assertEqual(data["price"], "3000.00")
        self.assertEqual(data["discount_type"], None)
        self.assertEqual(data["discount"], "0.00")

    def test_discount_with_exhausted_budget_is_not_applied(self):
        """A discount with no redemption budget left is ignored."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Used Up",
            scope="sitewide",
            discount_type="percent",
            value="20.00",
            starts_at=timezone.now() - timedelta(days=1),
            max_redemptions=5,
            redemption_count=5,
        )
        self.assertEqual(get_effective_price(variant)["price"], "5000.00")

    def test_out_of_window_discount_is_not_applied(self):
        """A discount past its end date does not reduce the price."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Past",
            scope="sitewide",
            discount_type="percent",
            value="20.00",
            starts_at=timezone.now() - timedelta(days=3),
            ends_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(get_effective_price(variant)["price"], "5000.00")

    def test_inactive_discount_is_not_applied(self):
        """An inactive discount does not reduce the price."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Off",
            scope="sitewide",
            discount_type="percent",
            value="20.00",
            starts_at=timezone.now() - timedelta(days=1),
            is_active=False,
        )
        self.assertEqual(get_effective_price(variant)["price"], "5000.00")

    def test_within_bundle_excludes_non_opt_in_discount(self):
        """Inside a bundle, only discounts opting in via applies_within_bundles apply."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Item Only",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            applies_within_bundles=False,
        )
        outside = get_effective_price(variant, within_bundle=False)
        inside = get_effective_price(variant, within_bundle=True)
        self.assertEqual(outside["price"], "4500.00")
        self.assertEqual(inside["price"], "5000.00")

    def test_within_bundle_applies_opt_in_discount(self):
        """A discount marked applies_within_bundles applies inside a bundle."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Bundle Friendly",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            applies_within_bundles=True,
        )
        inside = get_effective_price(variant, within_bundle=True)
        self.assertEqual(inside["price"], "4500.00")

    def test_result_is_cached(self):
        """A second read of an unchanged price hits the cache."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Cached",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        with CaptureQueriesContext(connection) as ctx:
            get_effective_price(variant)
            cold = len(ctx.captured_queries)
        with CaptureQueriesContext(connection) as ctx:
            get_effective_price(variant)
            warm = len(ctx.captured_queries)
        self.assertLess(warm, cold)

    def test_price_refreshes_after_discount_change(self):
        """Editing a discount invalidates cached effective prices."""
        _, variant = _make_product(price="5000.00")
        discount = create_discount(
            name="Malleable",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(get_effective_price(variant)["price"], "4500.00")
        discount.value = Decimal("20.00")
        discount.save()
        self.assertEqual(get_effective_price(variant)["price"], "4000.00")

    def test_price_refreshes_after_variant_price_change(self):
        """Editing a variant's price invalidates cached effective prices."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="On",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(get_effective_price(variant)["price"], "4500.00")
        variant.price = Decimal("6000.00")
        variant.save()
        data = get_effective_price(variant)
        self.assertEqual(data["base_price"], "6000.00")
        self.assertEqual(data["price"], "5400.00")


class BundleDiscountIntegrationTests(APITestCase):
    """Exercises discount integration into bundle pricing."""

    def setUp(self):
        cache.clear()

    def test_bundle_price_uses_discount_aware_component_prices(self):
        """A within-bundle discount raises the quoted bundle discount total."""

        def new_bundle_price(bundle):
            from apps.bundles.services import get_bundle_price

            return get_bundle_price(bundle)

        _, variant_a = _make_product(name="A", slug="a", sku="A-1", price="5000.00")
        _, variant_b = _make_product(name="B", slug="b", sku="B-1", price="7000.00")
        bundle = _make_bundle(variant_a, variant_b)
        create_discount(
            name="Bundle Item",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant_a.pk, variant_b.pk],
            applies_within_bundles=True,
        )
        price = new_bundle_price(bundle)
        self.assertEqual(price["regular_price"], "12000.00")
        self.assertEqual(price["price"], "10800.00")

    def test_bundle_excludes_non_opt_in_item_discount(self):
        """A non-opt-in item discount does not raise a bundle's regular price."""
        from apps.bundles.services import get_bundle_price

        _, variant_a = _make_product(name="A", slug="a-2", sku="A-2", price="5000.00")
        _, variant_b = _make_product(name="B", slug="b-2", sku="B-2", price="7000.00")
        bundle = _make_bundle(variant_a, variant_b)
        create_discount(
            name="Item Only",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant_a.pk],
            applies_within_bundles=False,
        )
        price = get_bundle_price(bundle)
        self.assertEqual(price["regular_price"], "12000.00")
        self.assertEqual(price["price"], "12000.00")

    def test_discount_change_invalidates_bundle_price_cache(self):
        """Creating a within-bundle discount refreshes a cached bundle price."""
        from apps.bundles.services import get_bundle_price

        _, variant_a = _make_product(name="A", slug="a-3", sku="A-3", price="5000.00")
        _, variant_b = _make_product(name="B", slug="b-3", sku="B-3", price="7000.00")
        bundle = _make_bundle(variant_a, variant_b)
        self.assertEqual(get_bundle_price(bundle)["regular_price"], "12000.00")
        create_discount(
            name="Later",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant_a.pk, variant_b.pk],
            applies_within_bundles=True,
        )
        price = get_bundle_price(bundle)
        self.assertEqual(price["regular_price"], "12000.00")
        self.assertEqual(price["price"], "10800.00")


class CouponServiceTests(APITestCase):
    """Exercises coupon validation and redemption accounting."""

    def setUp(self):
        cache.clear()

    def test_valid_coupon(self):
        """A coupon in its window and within limits validates."""
        coupon = _make_coupon()
        result = validate_coupon(coupon)
        self.assertTrue(result["valid"])
        self.assertEqual(result["discount_type"], "percent")
        self.assertEqual(result["value"], "10.00")
        self.assertIsNone(result["reason"])

    def test_inactive_coupon_invalid(self):
        """An inactive coupon is invalid."""
        coupon = _make_coupon(is_active=False)
        result = validate_coupon(coupon)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "Coupon is not active.")

    def test_expired_coupon_invalid(self):
        """A coupon outside its dates is invalid."""
        coupon = _make_coupon(ends_at=timezone.now() - timedelta(days=1))
        result = validate_coupon(coupon)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "Coupon is outside its valid dates.")

    def test_min_order_value_enforced(self):
        """A subtotal below the minimum invalidates the coupon."""
        coupon = _make_coupon(min_order_value="5000.00")
        result = validate_coupon(coupon, subtotal="4000.00")
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "Order value is below the coupon minimum.")
        self.assertTrue(validate_coupon(coupon, subtotal="5000.00")["valid"])

    def test_global_usage_limit_enforced(self):
        """An exhausted total usage limit invalidates the coupon."""
        coupon = _make_coupon(usage_limit_total=1)
        record_redemption(coupon)
        result = validate_coupon(coupon)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "Coupon usage limit has been reached.")

    def test_per_user_usage_limit_enforced(self):
        """An exhausted per-user limit invalidates the coupon for that user."""
        user = _make_user(email="a@example.com", username="alice")
        coupon = _make_coupon(usage_limit_per_user=1)
        record_redemption(coupon, user=user)
        result = validate_coupon(coupon, user=user)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "Coupon has already been used by this user.")
        other = _make_user(email="b@example.com", username="bob")
        self.assertTrue(validate_coupon(coupon, user=other)["valid"])

    def test_redemption_records_usage(self):
        """Recording a redemption creates a row that limits future use."""
        coupon = _make_coupon(usage_limit_total=2)
        redemption = record_redemption(coupon)
        self.assertEqual(CouponRedemption.objects.filter(coupon=coupon).count(), 1)
        self.assertIsNone(redemption.user)

    def test_coupon_stacking_rule(self):
        """A coupon does not stack over a discount unless marked stackable."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Sale",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        coupon = _make_coupon(code="STACK", discount_type="percent", value="10.00")
        data = get_effective_price(variant, coupon=coupon)
        self.assertEqual(data["price"], "4500.00")
        self.assertEqual(data["coupon_discount"], "0.00")

        coupon.stackable_with_discounts = True
        coupon.save()
        data = get_effective_price(variant, coupon=coupon)
        self.assertEqual(data["price"], "4050.00")
        self.assertEqual(data["coupon_discount"], "450.00")

    def test_coupon_applies_without_discount(self):
        """A coupon reduces the price when no automatic discount applies."""
        _, variant = _make_product(price="5000.00")
        coupon = _make_coupon(code="ONLY", discount_type="percent", value="10.00")
        data = get_effective_price(variant, coupon=coupon)
        self.assertEqual(data["price"], "4500.00")
        self.assertEqual(data["coupon_discount"], "500.00")

    def test_expired_coupon_contributes_nothing(self):
        """An expired coupon contributes no reduction to the price."""
        _, variant = _make_product(price="5000.00")
        coupon = _make_coupon(
            code="LATE",
            discount_type="percent",
            value="10.00",
            ends_at=timezone.now() - timedelta(days=1),
        )
        data = get_effective_price(variant, coupon=coupon)
        self.assertEqual(data["price"], "5000.00")
        self.assertEqual(data["coupon_discount"], "0.00")


class OnSaleCollectionTests(APITestCase):
    """Exercises the on-sale smart-collection rule."""

    def setUp(self):
        cache.clear()

    def _smart_on_sale(self, **kwargs):
        """Create a smart on-sale collection and return it."""
        return Collection.objects.create(
            name=kwargs.pop("name", "On Sale"),
            slug=kwargs.pop("slug", "on-sale"),
            collection_type="smart",
            smart_rule="on_sale",
            is_active=True,
        )

    def test_matches_products_with_active_discount(self):
        """Products with an active discount appear in an on-sale collection."""
        product_a, variant_a = _make_product(
            name="A", slug="os-a", sku="OS-A", price="5000.00"
        )
        product_b, variant_b = _make_product(
            name="B", slug="os-b", sku="OS-B", price="7000.00"
        )
        create_discount(
            name="On A",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant_a.pk],
        )
        collection = self._smart_on_sale()
        pks = compute_membership(collection)
        self.assertIn(product_a.pk, pks)
        self.assertNotIn(product_b.pk, pks)

    def test_excludes_bundle_scoped_discounts(self):
        """A bundle discount does not mark individual products on sale."""
        _, variant_a = _make_product(
            name="A", slug="osb-a", sku="OSB-A", price="5000.00"
        )
        _, variant_b = _make_product(
            name="B", slug="osb-b", sku="OSB-B", price="7000.00"
        )
        bundle = _make_bundle(variant_a, variant_b)
        create_discount(
            name="Bundle",
            scope="bundle",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            bundle=bundle.pk,
        )
        collection = self._smart_on_sale()
        pks = compute_membership(collection)
        self.assertEqual(pks, [])


class PromotionAdminApiTests(APITestCase):
    """Exercises admin CRUD for discounts and coupons and permissions."""

    def setUp(self):
        cache.clear()
        _make_admin()

    def test_anonymous_cannot_manage_discounts(self):
        """An unauthenticated caller is rejected from discount management."""
        response = self.client.get(URLS["admin_discounts"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_discounts(self):
        """A plain customer is rejected from creating a discount."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_discounts"],
            {
                "name": "B",
                "scope": "sitewide",
                "discount_type": "percent",
                "value": "10.00",
                "starts_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Discount.objects.count(), 0)

    def test_admin_can_create_sitewide_discount(self):
        """An admin can create a sitewide discount."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_discounts"],
            {
                "name": "Clearance",
                "scope": "sitewide",
                "discount_type": "percent",
                "value": "10.00",
                "starts_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Clearance")

    def test_scoped_discount_requires_relation(self):
        """A product-scoped discount without a product is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_discounts"],
            {
                "name": "B",
                "scope": "product",
                "discount_type": "percent",
                "value": "10.00",
                "starts_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bundle_scope_requires_bundle(self):
        """A bundle-scoped discount without a bundle is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_discounts"],
            {
                "name": "B",
                "scope": "bundle",
                "discount_type": "percent",
                "value": "10.00",
                "starts_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reversed_window_rejected(self):
        """A discount whose end predates its start is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_discounts"],
            {
                "name": "B",
                "scope": "sitewide",
                "discount_type": "percent",
                "value": "10.00",
                "starts_at": timezone.now().isoformat(),
                "ends_at": (timezone.now() - timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_can_create_and_update_coupon(self):
        """An admin can create and update a coupon."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_coupons"],
            {
                "code": "WELCOME10",
                "discount_type": "percent",
                "value": "10.00",
                "starts_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        coupon_pk = response.data["id"]
        url = reverse("api:promotions:admin-coupon-detail", args=[coupon_pk])
        response = self.client.patch(url, {"is_active": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_active"])

    def test_coupon_value_required_for_money_discount(self):
        """A fixed coupon without a value is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_coupons"],
            {
                "code": "BADFIX",
                "discount_type": "fixed",
                "starts_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_can_delete_discount(self):
        """An admin can delete a discount."""
        _login(self.client)
        _, variant = _make_product(price="5000.00")
        discount = create_discount(
            name="Gone",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant.pk],
        )
        url = reverse("api:promotions:admin-discount-detail", args=[discount.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Discount.objects.count(), 0)


class PublicApiTests(APITestCase):
    """Exercises the public effective-price and coupon validation endpoints."""

    def setUp(self):
        cache.clear()

    def test_effective_price_endpoint_returns_discount(self):
        """The effective-price endpoint returns the discounted figure."""
        _, variant = _make_product(price="5000.00")
        create_discount(
            name="Sale",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        url = reverse("api:promotions:variant-effective-price", args=[variant.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["price"], "4500.00")
        self.assertEqual(response.data["discount_name"], "Sale")

    def test_effective_price_endpoint_404_for_unknown_variant(self):
        """An unknown variant pk returns 404."""
        url = reverse("api:promotions:variant-effective-price", args=[999999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_coupon_validate_endpoint(self):
        """A valid coupon validates through the public endpoint."""
        _make_coupon(code="SAVE10")
        response = self.client.post(
            URLS["coupon_validate"], {"code": "SAVE10"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["valid"])

    def test_coupon_validate_unknown_code(self):
        """An unknown coupon code reports an invalid result."""
        response = self.client.post(
            URLS["coupon_validate"], {"code": "NOPE"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["valid"])
        self.assertEqual(response.data["reason"], "Unknown coupon code.")
