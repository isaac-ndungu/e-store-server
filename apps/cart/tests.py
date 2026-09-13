"""Tests for the cart app.

Covers the anonymous cookie-keyed cart: cart creation + cookie flags, adding
lines (merge, validation, server-side pricing), quantity updates, removal,
per-cart line isolation, and bad-cookie rotation.
"""

from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.bundles.models import Bundle
from apps.cart.models import Cart, CartItem
from apps.cart.selectors import CART_COOKIE_NAME
from apps.catalog.models import Brand, Category, Product, ProductVariant

CART_URL = reverse("api:cart:cart-detail")
ITEMS_URL = reverse("api:cart:cart-item-add")


def _make_variant(price="5000.00", active=True, stock_status="in_stock", seq=0):
    """Create an active product with one variant."""
    category, _ = Category.objects.get_or_create(name="Appliances", slug="appliances")
    brand, _ = Brand.objects.get_or_create(name="Samsung", slug="samsung")
    product = Product.objects.create(
        name=f"Kettle {seq}",
        slug=f"kettle-cart-{seq}",
        sku=f"KTL-CART-{seq}",
        description="A test product.",
        category=category,
        brand=brand,
        is_active=True,
    )
    return ProductVariant.objects.create(
        product=product,
        sku=f"KTL-CART-{seq}-V",
        attributes={"color": "Silver"},
        price=price,
        is_active=active,
        stock_status=stock_status,
    )


def _item_url(item_id):
    """Return the detail URL for a cart line."""
    return reverse("api:cart:cart-item-detail", kwargs={"item_id": item_id})


class CartRetrieveTests(APITestCase):
    """Exercises cart creation and reads."""

    def setUp(self):
        cache.clear()

    def test_get_creates_cart_and_sets_cookie(self):
        """A first visit creates a cart and sets the cookie."""
        response = self.client.get(CART_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Cart.objects.count(), 1)
        cookie = response.cookies[CART_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(response.data["item_count"], 0)
        self.assertEqual(response.data["items"], [])

    def test_cookie_carries_across_requests(self):
        """The same cookie resolves the same cart on a later read."""
        first = self.client.get(CART_URL)
        cart_id = first.data["id"]
        second = self.client.get(CART_URL)
        self.assertEqual(second.data["id"], cart_id)
        self.assertEqual(Cart.objects.count(), 1)

    def test_garbage_cookie_rotates_to_new_cart(self):
        """A forged cookie value yields a fresh cart, not an error."""
        self.client.cookies[CART_COOKIE_NAME] = "not-a-uuid"
        response = self.client.get(CART_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Cart.objects.count(), 1)


class CartAddTests(APITestCase):
    """Exercises adding lines to the cart."""

    def setUp(self):
        cache.clear()
        self.variant = _make_variant(seq=1)

    def test_anonymous_add_allowed(self):
        """An anonymous visitor can add a line without auth."""
        response = self.client.post(
            ITEMS_URL,
            {"variant_id": self.variant.pk, "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["item_count"], 2)
        self.assertEqual(len(response.data["items"]), 1)
        line = response.data["items"][0]
        self.assertEqual(line["sku"], self.variant.sku)
        self.assertEqual(line["quantity"], 2)

    def test_add_prices_server_side(self):
        """The line price comes from the catalogue, not the request."""
        response = self.client.post(
            ITEMS_URL,
            {"variant_id": self.variant.pk, "quantity": 2, "price": "1.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        line = response.data["items"][0]
        self.assertEqual(Decimal(line["unit_price"]), Decimal("5000.00"))
        self.assertEqual(Decimal(line["line_total"]), Decimal("10000.00"))
        self.assertEqual(Decimal(response.data["subtotal"]), Decimal("10000.00"))

    def test_add_merges_identical_line(self):
        """Adding the same variant twice bumps quantity on one line."""
        self.client.post(
            ITEMS_URL, {"variant_id": self.variant.pk, "quantity": 1}, format="json"
        )
        response = self.client.post(
            ITEMS_URL, {"variant_id": self.variant.pk, "quantity": 2}, format="json"
        )
        self.assertEqual(len(response.data["items"]), 1)
        self.assertEqual(response.data["items"][0]["quantity"], 3)
        self.assertEqual(CartItem.objects.count(), 1)

    def test_add_rejects_unknown_variant(self):
        """An unknown variant id is a 400, not an empty cart."""
        response = self.client.post(
            ITEMS_URL, {"variant_id": 999999, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_rejects_inactive_variant(self):
        """An inactive variant cannot be added."""
        variant = _make_variant(active=False, seq=2)
        response = self.client.post(
            ITEMS_URL, {"variant_id": variant.pk, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_rejects_out_of_stock_variant(self):
        """A staff-marked out-of-stock variant cannot be added."""
        variant = _make_variant(stock_status="out_of_stock", seq=3)
        response = self.client.post(
            ITEMS_URL, {"variant_id": variant.pk, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_rejects_bad_quantity(self):
        """Zero quantity fails serializer validation."""
        response = self.client.post(
            ITEMS_URL, {"variant_id": self.variant.pk, "quantity": 0}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_with_bundle(self):
        """A line can carry the bundle it was added from."""
        bundle = Bundle.objects.create(
            name="Starter Pack",
            slug="starter-pack",
            discount_type="percent",
            discount_value=Decimal("10.00"),
            is_active=True,
        )
        response = self.client.post(
            ITEMS_URL,
            {"variant_id": self.variant.pk, "quantity": 1, "bundle_id": bundle.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        line = response.data["items"][0]
        self.assertEqual(line["bundle_id"], bundle.pk)
        self.assertEqual(line["bundle_name"], "Starter Pack")


class CartUpdateTests(APITestCase):
    """Exercises quantity updates and removal."""

    def setUp(self):
        cache.clear()
        self.variant = _make_variant(seq=4)
        created = self.client.post(
            ITEMS_URL, {"variant_id": self.variant.pk, "quantity": 1}, format="json"
        )
        self.item_id = created.data["items"][0]["id"]

    def test_patch_updates_quantity(self):
        """PATCH sets the line quantity."""
        response = self.client.patch(
            _item_url(self.item_id), {"quantity": 5}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items"][0]["quantity"], 5)
        self.assertEqual(response.data["item_count"], 5)

    def test_patch_rejects_zero_quantity(self):
        """Zero quantity on update is a 400."""
        response = self.client.patch(
            _item_url(self.item_id), {"quantity": 0}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_cannot_touch_other_cart_line(self):
        """A line from another visitor's cart is not found (400)."""
        other_variant = _make_variant(seq=5)
        other_cart = Cart.objects.create()
        other_item = CartItem.objects.create(
            cart=other_cart, variant=other_variant, quantity=1
        )
        response = self.client.patch(
            _item_url(other_item.pk), {"quantity": 5}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        other_item.refresh_from_db()
        self.assertEqual(other_item.quantity, 1)

    def test_delete_removes_line(self):
        """DELETE drops the line and re-renders the cart."""
        response = self.client.delete(_item_url(self.item_id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["items"], [])
        self.assertEqual(CartItem.objects.count(), 0)

    def test_delete_unknown_line_is_400(self):
        """Deleting a missing line is a 400, not a 404 leak."""
        response = self.client.delete(_item_url(999999))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
