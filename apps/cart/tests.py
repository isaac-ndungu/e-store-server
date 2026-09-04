"""Tests for the cart app.

Covers service-layer logic (cart CRUD, item management, coupon handling,
pricing computation, wishlist) and the full API surface including
permissions, stock checks, and error handling.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.bundles.models import Bundle, BundleItem
from apps.cart.models import CartItem, WishlistItem
from apps.cart.services import (
    add_item,
    add_to_wishlist,
    apply_coupon,
    compute_cart_totals,
    get_or_create_cart,
    list_wishlist,
    merge_guest_cart,
    remove_coupon,
    remove_from_wishlist,
    remove_item,
    update_item_quantity,
)
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inventory.models import Inventory, Warehouse
from apps.promotions.models import Coupon
from apps.promotions.services import create_discount

_SEQ = [0]


def _make_user(email="buyer@example.com", username="buyer", **kwargs):
    """Create a plain customer user for tests."""
    return User.objects.create_user(
        email=email,
        username=username,
        password=kwargs.pop("password", "StrongPass123!"),
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        **kwargs,
    )


def _make_admin():
    """Create a staff superuser for admin tests."""
    return User.objects.create_user(
        email="manager@example.com",
        username="manager",
        password="ManagerPass123!",
        phone_number="+254700000000",
        is_staff=True,
        is_superuser=True,
    )


def _login(client, email="buyer@example.com", password="StrongPass123!"):
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
        **kwargs,
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
        slug=f"bundle-{_SEQ[0]}",
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


def _stock_variant(variant, quantity=100, warehouse_name="Main"):
    """Ensure a variant has stock in a warehouse."""
    warehouse, _ = Warehouse.objects.get_or_create(name=warehouse_name)
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity, "reserved": 0},
    )
    return warehouse


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


class CartCreationTests(APITestCase):
    """Exercises cart creation and retrieval for users and guests."""

    def setUp(self):
        cache.clear()

    def test_authenticated_user_gets_cart(self):
        """An authenticated user gets a cart tied to their account."""
        user = _make_user()
        cart = get_or_create_cart(user=user)
        self.assertIsNotNone(cart)
        self.assertEqual(cart.user_id, user.pk)
        self.assertEqual(cart.session_key, "")

    def test_guest_gets_cart_with_session_key(self):
        """A guest gets a cart identified by session key."""
        cart = get_or_create_cart(session_key="abc123")
        self.assertIsNotNone(cart)
        self.assertEqual(cart.session_key, "abc123")
        self.assertIsNone(cart.user_id)

    def test_returns_existing_cart(self):
        """Calling get_or_create_cart twice returns the same cart."""
        user = _make_user()
        cart1 = get_or_create_cart(user=user)
        cart2 = get_or_create_cart(user=user)
        self.assertEqual(cart1.pk, cart2.pk)

    def test_no_user_or_session_raises(self):
        """Providing neither user nor session key raises ValueError."""
        with self.assertRaises(ValueError):
            get_or_create_cart()

    def test_invalid_coupon_removed_on_retrieval(self):
        """An expired coupon is silently removed when the cart is accessed."""
        user = _make_user()
        coupon = _make_coupon(
            code="EXPIRED",
            ends_at=timezone.now() - timedelta(days=1),
        )
        cart = get_or_create_cart(user=user)
        cart.coupon = coupon
        cart.save(update_fields=["coupon"])
        retrieved = get_or_create_cart(user=user)
        self.assertIsNone(retrieved.coupon_id)


class MergeGuestCartTests(APITestCase):
    """Exercises adopting a guest cart into a user's cart on login."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        _, self.variant = _make_product()
        _stock_variant(self.variant)

    def test_no_session_key_returns_user_cart(self):
        """With no session key the user cart is returned untouched."""
        cart = merge_guest_cart(self.user, None)
        self.assertEqual(cart.user_id, self.user.pk)
        self.assertEqual(cart.items.count(), 0)

    def test_no_guest_cart_returns_user_cart(self):
        """With no matching guest cart the user cart is returned untouched."""
        cart = merge_guest_cart(self.user, "missing-session")
        self.assertEqual(cart.user_id, self.user.pk)
        self.assertEqual(cart.items.count(), 0)

    def test_guest_items_transfer_to_user_cart(self):
        """Guest lines are adopted into the user's cart on merge."""
        guest_cart = get_or_create_cart(session_key="guest-1")
        add_item(guest_cart, variant_id=self.variant.pk, quantity=2)
        merged = merge_guest_cart(self.user, "guest-1")
        item = merged.items.get(variant_id=self.variant.pk)
        self.assertEqual(item.quantity, 2)
        self.assertFalse(get_or_create_cart(session_key="guest-1").items.exists())

    def test_duplicate_lines_are_summed(self):
        """A guest line matching an existing user line sums quantities."""
        user_cart = get_or_create_cart(user=self.user)
        add_item(user_cart, variant_id=self.variant.pk, quantity=1)
        guest_cart = get_or_create_cart(session_key="guest-2")
        add_item(guest_cart, variant_id=self.variant.pk, quantity=3)
        merged = merge_guest_cart(self.user, "guest-2")
        self.assertEqual(merged.items.count(), 1)
        item = merged.items.get(variant_id=self.variant.pk)
        self.assertEqual(item.quantity, 4)

    def test_guest_cart_row_is_removed(self):
        """The adopted guest cart row is deleted after the merge."""
        guest_cart = get_or_create_cart(session_key="guest-3")
        add_item(guest_cart, variant_id=self.variant.pk, quantity=1)
        merge_guest_cart(self.user, "guest-3")
        from apps.cart.models import Cart

        self.assertFalse(Cart.objects.filter(session_key="guest-3").exists())

    def test_guest_coupon_adopted_when_user_cart_has_none(self):
        """A valid guest coupon carries over when the user cart has none."""
        coupon = _make_coupon(code="ADOPT10")
        guest_cart = get_or_create_cart(session_key="guest-4")
        add_item(guest_cart, variant_id=self.variant.pk, quantity=1)
        guest_cart.coupon = coupon
        guest_cart.save(update_fields=["coupon"])
        merged = merge_guest_cart(self.user, "guest-4")
        self.assertEqual(merged.coupon_id, coupon.pk)

    def test_user_coupon_is_not_overridden(self):
        """The user's existing coupon wins over a guest coupon."""
        user_coupon = _make_coupon(code="USERCOUP")
        guest_coupon = _make_coupon(code="GUESTCOP")
        user_cart = get_or_create_cart(user=self.user)
        user_cart.coupon = user_coupon
        user_cart.save(update_fields=["coupon"])
        guest_cart = get_or_create_cart(session_key="guest-5")
        guest_cart.coupon = guest_coupon
        guest_cart.save(update_fields=["coupon"])
        merged = merge_guest_cart(self.user, "guest-5")
        self.assertEqual(merged.coupon_id, user_coupon.pk)


class CartItemServiceTests(APITestCase):
    """Exercises adding, updating, and removing cart items."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self.cart = get_or_create_cart(user=self.user)

    def test_add_variant(self):
        """Adding a variant creates a cart item with quantity 1."""
        _, variant = _make_product()
        _stock_variant(variant)
        item = add_item(self.cart, variant_id=variant.pk)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.variant_id, variant.pk)

    def test_add_variant_with_quantity(self):
        """Adding a variant with a specified quantity stores it correctly."""
        _, variant = _make_product()
        _stock_variant(variant, quantity=10)
        item = add_item(self.cart, variant_id=variant.pk, quantity=3)
        self.assertEqual(item.quantity, 3)

    def test_duplicate_variant_merges_quantity(self):
        """Adding the same variant twice sums the quantities."""
        _, variant = _make_product()
        _stock_variant(variant, quantity=10)
        add_item(self.cart, variant_id=variant.pk, quantity=2)
        item = add_item(self.cart, variant_id=variant.pk, quantity=3)
        self.assertEqual(item.quantity, 5)

    def test_add_bundle(self):
        """Adding a bundle creates a cart item."""
        _, va = _make_product(name="A", slug="a", sku="A-1", price="3000.00")
        _, vb = _make_product(name="B", slug="b", sku="B-1", price="4000.00")
        bundle = _make_bundle(va, vb)
        item = add_item(self.cart, bundle_id=bundle.pk)
        self.assertEqual(item.bundle_id, bundle.pk)
        self.assertEqual(item.quantity, 1)

    def test_add_requires_target(self):
        """Adding an item without a variant or bundle raises ValidationError."""
        with self.assertRaises(ValidationError):
            add_item(self.cart)

    def test_add_both_targets_raises(self):
        """Providing both variant_id and bundle_id raises ValidationError."""
        _, variant = _make_product()
        _stock_variant(variant)
        _, va = _make_product(name="A", slug="a2", sku="A-2", price="3000.00")
        _, vb = _make_product(name="B", slug="b2", sku="B-2", price="4000.00")
        bundle = _make_bundle(va, vb)
        with self.assertRaises(ValidationError):
            add_item(self.cart, variant_id=variant.pk, bundle_id=bundle.pk)

    def test_inactive_variant_rejected(self):
        """Adding an inactive variant raises ValidationError."""
        _, variant = _make_product()
        variant.is_active = False
        variant.save()
        with self.assertRaises(ValidationError):
            add_item(self.cart, variant_id=variant.pk)

    def test_inactive_bundle_rejected(self):
        """Adding an inactive bundle raises ValidationError."""
        _, va = _make_product(name="A", slug="a3", sku="A-3", price="3000.00")
        _, vb = _make_product(name="B", slug="b3", sku="B-3", price="4000.00")
        bundle = _make_bundle(va, vb)
        bundle.is_active = False
        bundle.save()
        with self.assertRaises(ValidationError):
            add_item(self.cart, bundle_id=bundle.pk)

    def test_out_of_stock_rejected(self):
        """Adding a variant with insufficient stock raises ValidationError."""
        _, variant = _make_product()
        _stock_variant(variant, quantity=2)
        with self.assertRaises(ValidationError):
            add_item(self.cart, variant_id=variant.pk, quantity=5)

    def test_update_quantity(self):
        """Updating a cart item's quantity changes it."""
        _, variant = _make_product()
        _stock_variant(variant, quantity=10)
        item = add_item(self.cart, variant_id=variant.pk)
        updated = update_item_quantity(self.cart, item.pk, 7)
        self.assertEqual(updated.quantity, 7)

    def test_update_quantity_zero_removes(self):
        """Setting quantity to 0 removes the item."""
        _, variant = _make_product()
        _stock_variant(variant)
        item = add_item(self.cart, variant_id=variant.pk)
        result = update_item_quantity(self.cart, item.pk, 0)
        self.assertIsNone(result)
        self.assertFalse(CartItem.objects.filter(pk=item.pk).exists())

    def test_remove_item(self):
        """Removing an item deletes it from the cart."""
        _, variant = _make_product()
        _stock_variant(variant)
        item = add_item(self.cart, variant_id=variant.pk)
        remove_item(self.cart, item.pk)
        self.assertFalse(CartItem.objects.filter(pk=item.pk).exists())

    def test_remove_nonexistent_raises(self):
        """Removing an item that doesn't exist raises ValidationError."""
        with self.assertRaises(ValidationError):
            remove_item(self.cart, 999999)


class CouponServiceTests(APITestCase):
    """Exercises coupon apply and remove on carts."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self.cart = get_or_create_cart(user=self.user)

    def test_apply_valid_coupon(self):
        """A valid coupon is applied to the cart."""
        _, variant = _make_product()
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        coupon = _make_coupon(code="SAVE10")
        result = apply_coupon(self.cart, "SAVE10", user=self.user)
        self.assertTrue(result["valid"])
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.coupon_id, coupon.pk)

    def test_apply_invalid_coupon_raises(self):
        """An invalid coupon raises ValidationError."""
        with self.assertRaises(ValidationError):
            apply_coupon(self.cart, "NOPE")

    def test_apply_expired_coupon_raises(self):
        """An expired coupon raises ValidationError."""
        _make_coupon(
            code="LATE",
            ends_at=timezone.now() - timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            apply_coupon(self.cart, "LATE")

    def test_remove_coupon(self):
        """Removing a coupon clears it from the cart."""
        _, variant = _make_product()
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk)
        coupon = _make_coupon()
        self.cart.coupon = coupon
        self.cart.save(update_fields=["coupon"])
        remove_coupon(self.cart)
        self.cart.refresh_from_db()
        self.assertIsNone(self.cart.coupon_id)

    def test_remove_coupon_idempotent(self):
        """Removing a coupon when none is set is a no-op."""
        remove_coupon(self.cart)
        self.cart.refresh_from_db()
        self.assertIsNone(self.cart.coupon_id)


class CartTotalsTests(APITestCase):
    """Exercises the full cart totals computation."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self.cart = get_or_create_cart(user=self.user)

    def test_empty_cart_totals(self):
        """An empty cart returns zero totals."""
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["item_count"], 0)
        self.assertEqual(totals["subtotal"], "0.00")
        self.assertEqual(totals["total"], "0.00")

    def test_single_variant_totals(self):
        """A single variant item produces correct totals."""
        _, variant = _make_product(price="5000.00")
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=2)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["item_count"], 2)
        self.assertEqual(totals["subtotal"], "10000.00")
        self.assertEqual(len(totals["items"]), 1)

    def test_multiple_items_totals(self):
        """Multiple items produce correct aggregate totals."""
        _, va = _make_product(name="A", slug="a", sku="A-1", price="5000.00")
        _, vb = _make_product(name="B", slug="b", sku="B-1", price="3000.00")
        _stock_variant(va, quantity=10)
        _stock_variant(vb, quantity=10)
        add_item(self.cart, variant_id=va.pk, quantity=1)
        add_item(self.cart, variant_id=vb.pk, quantity=2)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["item_count"], 3)
        self.assertEqual(totals["subtotal"], "11000.00")

    def test_discount_reduces_totals(self):
        """An automatic discount reduces the effective subtotal."""
        _, variant = _make_product(price="5000.00")
        _stock_variant(variant)
        create_discount(
            name="Sale",
            scope="sitewide",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
        )
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["subtotal"], "4500.00")
        self.assertEqual(totals["discount_total"], "500.00")

    def test_coupon_reduces_totals(self):
        """A fixed coupon reduces the net goods subtotal, and total equals subtotal plus VAT."""
        _, variant = _make_product(price="10000.00")
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        coupon = _make_coupon(
            code="FLAT1000",
            discount_type="fixed",
            value="1000.00",
        )
        self.cart.coupon = coupon
        self.cart.save(update_fields=["coupon"])
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["subtotal"], "9000.00")
        self.assertEqual(totals["coupon_discount"], "1000.00")
        self.assertEqual(totals["coupon_code"], "FLAT1000")
        self.assertEqual(totals["vat_total"], "1440.00")
        self.assertEqual(totals["total"], "10440.00")

    def test_coupon_percent_discount(self):
        """A percentage coupon reduces the subtotal once; total is not double-discounted."""
        _, variant = _make_product(price="10000.00")
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        coupon = _make_coupon(
            code="PCT20",
            discount_type="percent",
            value="20.00",
        )
        self.cart.coupon = coupon
        self.cart.save(update_fields=["coupon"])
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["subtotal"], "8000.00")
        self.assertEqual(totals["coupon_discount"], "2000.00")
        self.assertEqual(totals["vat_total"], "1280.00")
        self.assertEqual(totals["total"], "9280.00")

    def test_coupon_nets_subtotal_without_double_counting(self):
        """The coupon is not subtracted from ``total`` again after netting the subtotal."""
        _, variant = _make_product(price="10000.00")
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        coupon = _make_coupon(
            code="NET20",
            discount_type="percent",
            value="20.00",
        )
        self.cart.coupon = coupon
        self.cart.save(update_fields=["coupon"])
        totals = compute_cart_totals(self.cart)
        self.assertEqual(
            Decimal(totals["total"]),
            Decimal(totals["subtotal"]) + Decimal(totals["vat_total"]),
        )

    def test_vat_standard_rate(self):
        """Standard-rated items include 16% VAT in the breakdown."""
        _, variant = _make_product(price="10000.00")
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(
            Decimal(totals["vat_breakdown"]["standard"]), Decimal("1600.00")
        )
        self.assertEqual(totals["vat_total"], "1600.00")

    def test_vat_zero_rated(self):
        """Zero-rated items contribute nothing to VAT."""
        product, variant = _make_product(price="10000.00")
        product.tax_class = "zero_rated"
        product.save(update_fields=["tax_class"])
        _stock_variant(variant)
        add_item(self.cart, variant_id=variant.pk, quantity=1)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["vat_breakdown"]["zero_rated"], "0.00")
        self.assertEqual(totals["vat_breakdown"]["standard"], "0.00")
        self.assertEqual(totals["vat_total"], "0.00")

    def test_vat_mixed_rates(self):
        """A cart mixing standard and zero-rated items splits VAT correctly."""
        product_a, va = _make_product(name="A", slug="va", sku="VA-1", price="10000.00")
        product_b, vb = _make_product(name="B", slug="vb", sku="VB-1", price="5000.00")
        product_b.tax_class = "zero_rated"
        product_b.save(update_fields=["tax_class"])
        _stock_variant(va, quantity=10)
        _stock_variant(vb, quantity=10)
        add_item(self.cart, variant_id=va.pk, quantity=1)
        add_item(self.cart, variant_id=vb.pk, quantity=1)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(
            Decimal(totals["vat_breakdown"]["standard"]), Decimal("1600.00")
        )
        self.assertEqual(
            Decimal(totals["vat_breakdown"]["zero_rated"]), Decimal("0.00")
        )
        self.assertEqual(totals["vat_total"], "1600.00")

    def test_bundle_item_totals(self):
        """A bundle item is priced through the bundle price service."""
        _, va = _make_product(name="A", slug="ba", sku="BA-1", price="3000.00")
        _, vb = _make_product(name="B", slug="bb", sku="BB-1", price="4000.00")
        _stock_variant(va, quantity=10)
        _stock_variant(vb, quantity=10)
        bundle = _make_bundle(va, vb, discount_type="percent", discount_value="10.00")
        add_item(self.cart, bundle_id=bundle.pk, quantity=1)
        totals = compute_cart_totals(self.cart)
        self.assertEqual(totals["item_count"], 1)
        self.assertNotEqual(totals["subtotal"], "0.00")


class WishlistServiceTests(APITestCase):
    """Exercises wishlist add, remove, and list operations."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()

    def test_add_to_wishlist(self):
        """Adding a product creates a wishlist item."""
        product, _ = _make_product()
        item = add_to_wishlist(self.user, product.pk)
        self.assertEqual(item.user_id, self.user.pk)
        self.assertEqual(item.product_id, product.pk)

    def test_add_duplicate_is_noop(self):
        """Adding the same product twice returns the existing item."""
        product, _ = _make_product()
        item1 = add_to_wishlist(self.user, product.pk)
        item2 = add_to_wishlist(self.user, product.pk)
        self.assertEqual(item1.pk, item2.pk)

    def test_remove_from_wishlist(self):
        """Removing a product deletes the wishlist item."""
        product, _ = _make_product()
        add_to_wishlist(self.user, product.pk)
        remove_from_wishlist(self.user, product.pk)
        self.assertFalse(
            WishlistItem.objects.filter(user=self.user, product=product).exists()
        )

    def test_remove_nonexistent_raises(self):
        """Removing a product not in the wishlist raises ValidationError."""
        product, _ = _make_product()
        with self.assertRaises(ValidationError):
            remove_from_wishlist(self.user, product.pk)

    def test_list_wishlist(self):
        """list_wishlist returns the user's items with products pre-fetched."""
        p1, _ = _make_product(name="A", slug="wl-a", sku="WL-A")
        p2, _ = _make_product(name="B", slug="wl-b", sku="WL-B")
        add_to_wishlist(self.user, p1.pk)
        add_to_wishlist(self.user, p2.pk)
        items = list(list_wishlist(self.user))
        self.assertEqual(len(items), 2)


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class CartApiTests(APITestCase):
    """Exercises the full cart API surface."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self._product, self._variant = _make_product(price="5000.00")
        _stock_variant(self._variant, quantity=10)

    def test_get_cart_authenticated(self):
        """An authenticated user can retrieve their cart."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.get(reverse("api:cart:cart"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("items", response.data)
        self.assertIn("subtotal", response.data)

    def test_get_cart_guest_with_session(self):
        """An anonymous guest gets a cart keyed to their Django session."""
        response = self.client.get(reverse("api:cart:cart"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("items", response.data)

    def test_get_cart_anonymous_gets_empty_cart(self):
        """An anonymous request is auto-issued a session and an empty cart."""
        response = self.client.get(reverse("api:cart:cart"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["item_count"], 0)
        self.assertEqual(response.data["subtotal"], "0.00")

    def test_add_item_to_cart(self):
        """POST /cart/items/ adds a variant to the cart."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("item_id", response.data)

    def test_add_item_guest(self):
        """An anonymous guest can add items to their session cart."""
        response = self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_login_merges_guest_cart(self):
        """Logging in adopts the items added to the same session as a guest."""
        self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 2},
            format="json",
        )
        login_url = reverse("api:accounts:login")
        response = self.client.post(
            login_url,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        cart_response = self.client.get(reverse("api:cart:cart"))
        self.assertEqual(cart_response.status_code, status.HTTP_200_OK)
        self.assertEqual(cart_response.data["item_count"], 2)

    def test_update_cart_item_quantity(self):
        """PATCH /cart/items/{id}/ updates the quantity."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        add_response = self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        item_id = add_response.data["item_id"]
        response = self.client.patch(
            reverse("api:cart:cart-item-detail", args=[item_id]),
            {"quantity": 5},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_remove_cart_item(self):
        """DELETE /cart/items/{id}/ removes the item."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        add_response = self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        item_id = add_response.data["item_id"]
        response = self.client.delete(
            reverse("api:cart:cart-item-detail", args=[item_id]),
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_apply_coupon(self):
        """POST /cart/apply-coupon/ applies a valid coupon."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        _make_coupon(code="API10")
        response = self.client.post(
            reverse("api:cart:cart-apply-coupon"),
            {"code": "API10"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["valid"])

    def test_apply_invalid_coupon(self):
        """POST /cart/apply-coupon/ with an invalid code returns 400."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            reverse("api:cart:cart-apply-coupon"),
            {"code": "BOGUS"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remove_coupon(self):
        """DELETE /cart/remove-coupon/ clears the coupon."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        coupon = _make_coupon(code="RM10")
        cart = get_or_create_cart(user=self.user)
        cart.coupon = coupon
        cart.save(update_fields=["coupon"])
        response = self.client.delete(reverse("api:cart:cart-remove-coupon"))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        cart.refresh_from_db()
        self.assertIsNone(cart.coupon_id)

    def test_empty_cart(self):
        """DELETE /cart/ removes all items and clears the coupon."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        coupon = _make_coupon(code="EMPTY10")
        cart = get_or_create_cart(user=self.user)
        cart.coupon = coupon
        cart.save(update_fields=["coupon"])
        self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 2},
            format="json",
        )
        response = self.client.delete(reverse("api:cart:cart"))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        cart.refresh_from_db()
        self.assertEqual(cart.items.count(), 0)
        self.assertIsNone(cart.coupon_id)

    def test_cart_totals_in_response(self):
        """GET /cart/ includes computed totals in the response."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 2},
            format="json",
        )
        response = self.client.get(reverse("api:cart:cart"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["subtotal"], "10000.00")
        self.assertEqual(response.data["item_count"], 2)
        self.assertIn("vat_breakdown", response.data)
        self.assertIn("total", response.data)


class WishlistApiTests(APITestCase):
    """Exercises the wishlist API surface."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self._product, _ = _make_product(name="Wish", slug="wish", sku="WISH-1")

    def test_get_wishlist_authenticated(self):
        """An authenticated user can retrieve their wishlist."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.get(reverse("api:cart:wishlist"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)

    def test_get_wishlist_anonymous_rejected(self):
        """An anonymous user is rejected from the wishlist."""
        response = self.client.get(reverse("api:cart:wishlist"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_add_to_wishlist(self):
        """POST /wishlist/ adds a product."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            reverse("api:cart:wishlist"),
            {"product_id": self._product.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_add_nonexistent_product(self):
        """POST /wishlist/ with an unknown product returns 400."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            reverse("api:cart:wishlist"),
            {"product_id": 999999},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_remove_from_wishlist(self):
        """DELETE /wishlist/{product_id}/ removes the product."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        self.client.post(
            reverse("api:cart:wishlist"),
            {"product_id": self._product.pk},
            format="json",
        )
        response = self.client.delete(
            reverse("api:cart:wishlist-item-detail", args=[self._product.pk]),
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_remove_nonexistent_from_wishlist(self):
        """DELETE /wishlist/{product_id}/ for a product not in wishlist returns 400."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.delete(
            reverse("api:cart:wishlist-item-detail", args=[self._product.pk]),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GuestCartSessionIsolationTests(APITestCase):
    """Exercises that guest carts are isolated by Django session."""

    def setUp(self):
        cache.clear()
        self._product, self._variant = _make_product(price="5000.00")
        _stock_variant(self._variant, quantity=100)

    def test_different_sessions_different_carts(self):
        """Two different browser sessions produce separate carts."""
        client_a = APIClient()
        client_b = APIClient()
        response_a = client_a.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        response_b = client_b.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 2},
            format="json",
        )
        self.assertEqual(response_a.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response_b.status_code, status.HTTP_201_CREATED)

        cart_a = client_a.get(reverse("api:cart:cart"))
        cart_b = client_b.get(reverse("api:cart:cart"))
        self.assertEqual(cart_a.data["item_count"], 1)
        self.assertEqual(cart_b.data["item_count"], 2)

    def test_guest_cannot_see_other_guest_cart(self):
        """A guest with a different session cannot access another's cart."""
        self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        stranger = APIClient()
        response = stranger.get(reverse("api:cart:cart"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["item_count"], 0)


class SecurityTests(APITestCase):
    """Exercises security invariants for the cart endpoints."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self._product, self._variant = _make_product(price="5000.00")
        _stock_variant(self._variant, quantity=10)

    def test_no_client_supplied_price(self):
        """The cart does not accept or store a client-supplied price."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            reverse("api:cart:cart-items"),
            {
                "variant_id": self._variant.pk,
                "quantity": 1,
                "unit_price": "1.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        cart_response = self.client.get(reverse("api:cart:cart"))
        for item in cart_response.data["items"]:
            if item.get("variant_id") == self._variant.pk:
                self.assertNotEqual(item["unit_price"], "1.00")

    def test_stock_enforced_on_add(self):
        """Adding more than available stock is rejected."""
        _stock_variant(self._variant, quantity=2)
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 5},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_coupon_usage_limit_enforced(self):
        """A coupon that has reached its usage limit is rejected."""
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        self.client.post(
            reverse("api:cart:cart-items"),
            {"variant_id": self._variant.pk, "quantity": 1},
            format="json",
        )
        coupon = _make_coupon(code="USED", usage_limit_total=1)
        from apps.promotions.services import record_redemption

        record_redemption(coupon)
        response = self.client.post(
            reverse("api:cart:cart-apply-coupon"),
            {"code": "USED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
