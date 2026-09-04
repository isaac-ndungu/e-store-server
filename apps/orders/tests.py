"""Tests for the orders app.

Covers the full COD checkout path for logged-in and guest users: idempotent
order creation, stock reservation ownership, server-side pricing, OTP
verification (expiry, attempts, terminal states), guest/owner access control,
cancellation releasing stock, and per-order status-history auditing.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.cart.services import add_item, get_or_create_cart
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inventory.models import Inventory, StockReservation, Warehouse
from apps.orders.models import Order, OrderVerification
from apps.orders.services import create_order_from_cart, requires_otp_for_payment

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


def _login(client, email="buyer@example.com", password="StrongPass123!"):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_product(name="Kettle", slug="kettle", sku="KTL", price="5000.00", **kwargs):
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


def _stock_variant(variant, quantity=100, warehouse_name="Main"):
    """Ensure a variant has stock in a warehouse."""
    warehouse, _ = Warehouse.objects.get_or_create(name=warehouse_name)
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity, "reserved": 0},
    )
    return warehouse


def _guest_cart(session_key="guest-session-1"):
    """Return a guest cart identified by a session key."""
    return get_or_create_cart(session_key=session_key)


def _guest_cart_for_client(client, variant, quantity=1):
    """Add stock items to the guest cart keyed to the test client's session.

    The view resolves the guest cart from the request's Django session key, so
    the cart must be created against that same key rather than a hard-coded
    one to be visible to the view.

    Args:
        client (APIClient): the test client.
        variant (ProductVariant): the variant to add.
        quantity (int): the quantity to add.

    Returns:
        Cart: the guest cart now containing the item.
    """
    session_key = client.session.session_key
    if session_key is None:
        client.session.save()
        session_key = client.session.session_key
    cart = get_or_create_cart(session_key=session_key)
    add_item(cart, variant_id=variant.pk, quantity=quantity)
    return cart


def _mock_sms():
    """Return a stub for the SMS provider that reports a successful send.

    The mocked provider is given a real success payload so the notification
    service can record a ``sent`` audit log without an external network call.
    """
    return mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "test-message-id",
            "response": {"SMSMessageData": {"NumSegments": 1}},
            "segments": 1,
            "error": "",
        },
    )


class OrderModelTests(APITestCase):
    """Exercises order model invariants."""

    def setUp(self):
        cache.clear()

    def test_order_status_history_created_on_create(self):
        """A freshly created order has a single pending history row."""
        _, variant = _make_product()
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart, user=user, phone="+254712345678", payment_method="cod"
            )
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.status_history.count(), 1)
        history = order.status_history.first()
        self.assertEqual(history.from_status, "")
        self.assertEqual(history.to_status, "pending")

    def test_cod_requires_otp(self):
        """COD orders require OTP verification."""
        _, variant = _make_product()
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart, user=user, phone="+254712345678", payment_method="cod"
            )
        self.assertTrue(requires_otp_for_payment(order))

    def test_mpesa_does_not_require_cod_otp(self):
        """Non-COD orders skip the COD OTP flow."""
        _, variant = _make_product()
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart, user=user, phone="+254712345678", payment_method="mpesa"
            )
        self.assertFalse(requires_otp_for_payment(order))

    def test_order_item_snapshots_product_data(self):
        """Order items snapshot catalogue data at checkout time."""
        _, variant = _make_product(price="3000.00")
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=2)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart, user=user, phone="+254712345678", payment_method="cod"
            )
        item = order.items.get()
        self.assertEqual(item.product_name, variant.product.name)
        self.assertEqual(item.variant_sku, variant.sku)
        self.assertEqual(item.unit_price, Decimal("3000.00"))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.total_price, Decimal("6000.00"))


class OrderCreationTests(APITestCase):
    """Exercises the POST order creation endpoint."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        self.url = reverse("api:orders:order-list")

    def test_guest_can_place_order_with_idempotency_key(self):
        """A guest can place an order with an Idempotency-Key header."""
        _, variant = _make_product()
        _stock_variant(variant)
        _guest_cart_for_client(self.client, variant, quantity=1)
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                HTTP_IDEMPOTENCY_KEY="guest-order-1",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "pending")
        self.assertTrue(response.data["requires_otp"])
        self.assertTrue(Order.objects.filter(phone=self.phone).exists())

    def test_authenticated_user_can_place_order(self):
        """An authenticated user can place an order tied to their account."""
        _, variant = _make_product()
        _stock_variant(variant)
        user = _make_user()
        _login(self.client)
        # Authenticated order creation reads the logged-in user's cart.
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                HTTP_IDEMPOTENCY_KEY=f"order-{user.pk}-fixed",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.user_id, user.pk)

    def test_missing_idempotency_key_rejected(self):
        """Placing an order without an Idempotency-Key header is rejected."""
        _, variant = _make_product()
        _stock_variant(variant)
        _guest_cart_for_client(self.client, variant, quantity=1)
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Idempotency-Key", str(response.data))

    def test_repeated_key_reuses_cached_order(self):
        """Repeating an Idempotency-Key returns the prior order (no dup)."""
        _, variant = _make_product()
        _stock_variant(variant)
        _guest_cart_for_client(self.client, variant, quantity=1)
        with _mock_sms():
            first = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                HTTP_IDEMPOTENCY_KEY="dup-order",
                format="json",
            )
            second = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                HTTP_IDEMPOTENCY_KEY="dup-order",
                format="json",
            )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_empty_cart_rejected(self):
        """Placing an order from an empty cart returns 400."""
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                HTTP_IDEMPOTENCY_KEY="empty-order",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrderAccessTests(APITestCase):
    """Exercises ownership and role-based access to order endpoints."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        with _mock_sms():
            cart = _guest_cart("owner-session")
            add_item(cart, variant_id=self.variant.pk, quantity=1)
            self.order = create_order_from_cart(
                cart=cart, user=None, phone=self.phone, payment_method="cod"
            )

    def test_unauth_list_returns_empty(self):
        """An anonymous caller gets an empty order list (no user row)."""
        response = self.client.get(reverse("api:orders:order-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_authenticated_user_cannot_read_others_order(self):
        """A different logged-in user cannot read another user's order."""
        _make_user(email="other@example.com", username="otheruser")
        _login(self.client, email="other@example.com")
        detail = reverse("api:orders:order-detail", args=[self.order.pk])
        response = self.client.get(detail)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_authenticated_user_cannot_cancel_others_order(self):
        """A different logged-in user cannot cancel another user's order."""
        _make_user(email="other@example.com", username="otheruser")
        _login(self.client, email="other@example.com")
        detail = reverse("api:orders:order-detail", args=[self.order.pk])
        response = self.client.delete(detail)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_guest_requires_matching_phone_to_view_order(self):
        """A guest cannot view an order without a matching contact phone."""
        detail = reverse("api:orders:order-detail", args=[self.order.pk])
        # No phone query parameter.
        response = self.client.get(detail)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Wrong phone.
        response = self.client.get(detail, {"phone": "+254700000001"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_guest_with_matching_phone_can_view_order(self):
        """A guest with the matching contact phone can view the order."""
        detail = reverse("api:orders:order-detail", args=[self.order.pk])
        response = self.client.get(detail, {"phone": self.phone})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], self.order.pk)

    def test_missing_order_returns_404(self):
        """A non-existent order id returns 404 for the owning phone."""
        detail = reverse("api:orders:order-detail", args=[9999])
        response = self.client.get(detail, {"phone": self.phone})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OrderOTPFlowTests(APITestCase):
    """Exercises the COD OTP verification and resend flow."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        with _mock_sms():
            cart = _guest_cart("otp-session")
            add_item(cart, variant_id=self.variant.pk, quantity=1)
            self.order = create_order_from_cart(
                cart=cart, user=None, phone=self.phone, payment_method="cod"
            )
            self.verification = OrderVerification.objects.create(
                order=self.order,
                otp_code="123456",
                phone_number=self.phone,
                status="pending",
            )
        self.verify_url = (
            reverse("api:orders:order-verify-otp", args=[self.order.pk])
            + f"?phone={self.phone}"
        )
        self.resend_url = (
            reverse("api:orders:order-otp-resend", args=[self.order.pk])
            + f"?phone={self.phone}"
        )

    def test_verify_with_correct_code_confirms_order(self):
        """Submitting the correct code confirms the order and fulfils stock."""
        with _mock_sms():
            response = self.client.post(
                self.verify_url, {"otp_code": "123456"}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.status, "verified")

    def test_verify_with_wrong_code_fails(self):
        """Submitting the wrong code returns 400 and increments attempts."""
        with _mock_sms():
            response = self.client.post(
                self.verify_url, {"otp_code": "999999"}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.attempts, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")

    def test_verify_requires_matching_phone(self):
        """A guest must supply the order's contact phone to verify it."""
        url = reverse("api:orders:order-verify-otp", args=[self.order.pk])
        with _mock_sms():
            response = self.client.post(url, {"otp_code": "123456"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_expired_code_marks_expired(self):
        """An expired code is rejected and marks the record expired."""
        self.verification.sent_at = timezone.now() - timedelta(minutes=30)
        self.verification.save(update_fields=["sent_at"])
        with _mock_sms():
            response = self.client.post(
                self.verify_url, {"otp_code": "123456"}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.status, "expired")

    def test_max_attempts_marks_failed(self):
        """Reaching the attempt limit marks the record failed."""
        self.verification.attempts = OrderVerification.MAX_ATTEMPTS
        self.verification.save(update_fields=["attempts"])
        with _mock_sms():
            response = self.client.post(
                self.verify_url, {"otp_code": "123456"}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.status, "failed")

    def test_resend_generates_new_code(self):
        """Resending generates a fresh code and resets attempts."""
        old_code = self.verification.otp_code
        self.verification.attempts = 2
        self.verification.save(update_fields=["attempts"])
        with _mock_sms():
            response = self.client.post(self.resend_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.attempts, 0)
        self.assertEqual(self.verification.status, "pending")
        self.assertNotEqual(self.verification.otp_code, old_code)

    def test_resend_on_non_otp_order_rejected(self):
        """Resending OTP for a non-COD order returns 400."""
        self.order.payment_method = "mpesa"
        self.order.save(update_fields=["payment_method"])
        with _mock_sms():
            response = self.client.post(self.resend_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class StockReservationTests(APITestCase):
    """Exercises reservation ownership and release on cancellation."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product(price="2000.00")
        self.warehouse = _stock_variant(self.variant, quantity=50)

    def test_reservation_links_to_order_item(self):
        """Reservations created at checkout are owned by the order item."""
        with _mock_sms():
            cart = _guest_cart("res-session")
            add_item(cart, variant_id=self.variant.pk, quantity=3)
            order = create_order_from_cart(
                cart=cart, user=None, phone=self.phone, payment_method="cod"
            )
        item = order.items.get()
        reservation = StockReservation.objects.filter(order_item=item).first()
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.order_item_id, item.pk)
        self.assertEqual(reservation.quantity, 3)

    def test_cancel_releases_reserved_stock(self):
        """Cancelling a pending order releases its reservation."""
        with _mock_sms():
            cart = _guest_cart("cancel-session")
            add_item(cart, variant_id=self.variant.pk, quantity=5)
            order = create_order_from_cart(
                cart=cart, user=None, phone=self.phone, payment_method="cod"
            )
        item = order.items.get()
        reservation = StockReservation.objects.get(order_item=item)
        self.assertEqual(reservation.status, "active")
        before = Inventory.objects.get(variant=self.variant, warehouse=self.warehouse)
        self.assertEqual(before.reserved, 5)

        with _mock_sms():
            url = (
                reverse("api:orders:order-detail", args=[order.pk])
                + f"?phone={self.phone}"
            )
            response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, "released")
        after = Inventory.objects.get(variant=self.variant, warehouse=self.warehouse)
        self.assertEqual(after.reserved, 0)


class OrderHistoryAuditTests(APITestCase):
    """Exercises the status-history endpoint."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        with _mock_sms():
            cart = _guest_cart("history-session")
            add_item(cart, variant_id=self.variant.pk, quantity=1)
            self.order = create_order_from_cart(
                cart=cart, user=None, phone=self.phone, payment_method="cod"
            )

    def test_status_history_endpoint_returns_audit_trail(self):
        """The status-history endpoint returns the order's transitions."""
        url = reverse("api:orders:order-status-history", args=[self.order.pk])
        response = self.client.get(url, {"phone": self.phone})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["to_status"], "pending")

    def test_status_history_requires_ownership(self):
        """Another caller's order history is not accessible without ownership."""
        url = reverse("api:orders:order-status-history", args=[self.order.pk])
        response = self.client.get(url, {"phone": "+254799999999"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
