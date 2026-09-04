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
from django.core.exceptions import ValidationError
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
from apps.promotions.models import Coupon, CouponRedemption

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
    package_weight = Decimal(kwargs.pop("package_weight", "2.00"))
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
        package_weight=package_weight,
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


def _active_zone():
    """Return a reusable active delivery zone for physical orders."""
    from apps.shipping.models import DeliveryZone

    zone, _ = DeliveryZone.objects.get_or_create(
        county="Nairobi",
        area_name="Westlands",
        defaults={"base_fee": "200.00", "per_kg_rate": "50.00"},
    )
    return zone


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
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
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
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        self.assertTrue(requires_otp_for_payment(order))

    def test_mpesa_does_not_require_cod_otp(self):
        """Non-COD payment methods skip the COD OTP flow.

        The payment adapter reports that a provider method does not need COD
        OTP verification. But with no gateway wired, an M-Pesa order cannot be
        placed at all — it is rejected before creation rather than left pending
        holding stock with no way to confirm or refund it.
        """
        from apps.orders.payments import requires_otp_for_payment_method

        self.assertFalse(requires_otp_for_payment_method("mpesa"))
        self.assertFalse(requires_otp_for_payment_method("card"))
        self.assertTrue(requires_otp_for_payment_method("cod"))
        with self.assertRaises(ValidationError):
            _, variant = _make_product()
            _stock_variant(variant)
            user = _make_user()
            cart = get_or_create_cart(user=user)
            add_item(cart, variant_id=variant.pk, quantity=1)
            with _mock_sms():
                create_order_from_cart(
                    cart=cart, user=user, phone="+254712345678", payment_method="mpesa"
                )

    def test_order_item_snapshots_product_data(self):
        """Order items snapshot catalogue data at checkout time."""
        _, variant = _make_product(price="3000.00")
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=2)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        item = order.items.get()
        self.assertEqual(item.product_name, variant.product.name)
        self.assertEqual(item.variant_sku, variant.sku)
        self.assertEqual(item.unit_price, Decimal("3000.00"))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.total_price, Decimal("6000.00"))

    def test_physical_order_without_zone_rejected(self):
        """A cart of physical items cannot be ordered without a delivery zone."""
        _, variant = _make_product()
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with self.assertRaises(ValidationError):  # noqa: SIM117
            with _mock_sms():
                create_order_from_cart(
                    cart=cart, user=user, phone="+254712345678", payment_method="cod"
                )

    def test_digital_only_order_needs_no_zone(self):
        """A cart containing only digital items needs no delivery zone."""
        _, variant = _make_product(product_type="digital")
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart, user=user, phone="+254712345678", payment_method="cod"
            )
        self.assertEqual(order.status, "pending")

    def test_applied_discount_snapshotted_per_line(self):
        """Checkout snapshots the per-line action discount amount."""
        _, variant = _make_product(price="5000.00")
        _stock_variant(variant)
        from apps.promotions.services import create_discount

        create_discount(
            name="Spring Sale",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant.pk],
        )
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=2)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("4500.00"))
        self.assertEqual(item.applied_discount, Decimal("1000.00"))

    def test_applied_discount_zero_when_no_promotion(self):
        """A line with no active promotion records zero applied discount."""
        _, variant = _make_product(price="3000.00")
        _stock_variant(variant)
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        item = order.items.get()
        self.assertEqual(item.applied_discount, Decimal("0.00"))


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
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
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
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
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
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
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
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
                HTTP_IDEMPOTENCY_KEY="dup-order",
                format="json",
            )
            second = self.client.post(
                self.url,
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
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
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
                HTTP_IDEMPOTENCY_KEY="empty-order",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_physical_order_without_zone_rejected_http(self):
        """A physical order submitted without a delivery zone returns 400."""
        _, variant = _make_product()
        _stock_variant(variant)
        _guest_cart_for_client(self.client, variant, quantity=1)
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "cod"},
                HTTP_IDEMPOTENCY_KEY="no-zone-http",
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
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
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

    def test_guest_requires_valid_token_to_view_order(self):
        """A guest cannot view an order without its lookup token."""
        from uuid import uuid4

        # A random (wrong) token is not this order.
        wrong = reverse("api:orders:order-detail", args=[uuid4()])
        self.assertEqual(self.client.get(wrong).status_code, status.HTTP_404_NOT_FOUND)

    def test_guest_with_valid_token_can_view_order(self):
        """A guest holding the order's lookup token can view the order."""
        detail = reverse("api:orders:order-detail", args=[self.order.lookup_token])
        response = self.client.get(detail)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], self.order.pk)

    def test_missing_order_returns_404(self):
        """A non-existent lookup token returns 404."""
        from uuid import uuid4

        detail = reverse("api:orders:order-detail", args=[uuid4()])
        response = self.client.get(detail)
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
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
            self.verification = OrderVerification.objects.create(
                order=self.order,
                otp_code="123456",
                phone_number=self.phone,
                status="pending",
            )
        self.verify_url = reverse(
            "api:orders:order-verify-otp", args=[self.order.lookup_token]
        )
        self.resend_url = reverse(
            "api:orders:order-otp-resend", args=[self.order.lookup_token]
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

    def test_verify_requires_valid_token(self):
        """A guest must hold the order's token to verify it."""
        from uuid import uuid4

        url = reverse("api:orders:order-verify-otp", args=[uuid4()])
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

    def test_resend_generates_new_code_and_preserves_attempts(self):
        """Resending issues a fresh code and preserves the attempt counter.

        The attempt counter is deliberately not reset on resend so an attacker
        cannot extend the brute-force budget by re-sending codes; only the
        code and its expiry clock change. The last send is pushed back to just
        past the cooldown so the resend is permitted.
        """
        old_code = self.verification.otp_code
        self.verification.attempts = 2
        self.verification.sent_at = timezone.now() - timedelta(minutes=1)
        self.verification.save(update_fields=["attempts", "sent_at"])
        with _mock_sms():
            response = self.client.post(self.resend_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.attempts, 2)
        self.assertEqual(self.verification.status, "pending")
        self.assertNotEqual(self.verification.otp_code, old_code)
        self.assertEqual(self.verification.resend_count, 1)

    def test_resend_within_cooldown_rejected(self):
        """A resend within the cooldown window returns 400."""
        self.verification.sent_at = timezone.now()
        self.verification.save(update_fields=["sent_at"])
        with _mock_sms():
            response = self.client.post(self.resend_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resend_cap_reached_rejected(self):
        """Once the resend cap is reached, further resends are rejected."""
        self.verification.resend_count = OrderVerification.MAX_RESENDS
        self.verification.sent_at = timezone.now() - timedelta(minutes=1)
        self.verification.save(update_fields=["resend_count", "sent_at"])
        with _mock_sms():
            response = self.client.post(self.resend_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
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
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        item = order.items.get()
        reservation = StockReservation.objects.get(order_item=item)
        self.assertEqual(reservation.status, "active")
        before = Inventory.objects.get(variant=self.variant, warehouse=self.warehouse)
        self.assertEqual(before.reserved, 5)

        with _mock_sms():
            url = reverse("api:orders:order-detail", args=[order.lookup_token])
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
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )

    def test_status_history_endpoint_returns_audit_trail(self):
        """The status-history endpoint returns the order's transitions."""
        url = reverse("api:orders:order-status-history", args=[self.order.lookup_token])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["to_status"], "pending")

    def test_status_history_requires_valid_token(self):
        """Another caller's order history is not accessible without the token."""
        from uuid import uuid4

        url = reverse("api:orders:order-status-history", args=[uuid4()])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class CouponRedemptionTests(APITestCase):
    """Exercises coupon usage-limit recording at order confirmation."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product(price="5000.00")
        _stock_variant(self.variant, quantity=10)
        self.coupon = Coupon.objects.create(
            code="SAVE20",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=10),
            is_active=True,
            usage_limit_total=1,
        )

    def _confirm_order(self):
        """Create and confirm a COD order using the coupon."""
        from apps.cart.services import apply_coupon

        with _mock_sms():
            cart = _guest_cart("coupon-session")
            add_item(cart, variant_id=self.variant.pk, quantity=1)
            apply_coupon(cart, self.coupon.code)
            order = create_order_from_cart(
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
            from apps.orders.services import confirm_order_from_verification

            confirmed = confirm_order_from_verification(order)
        return confirmed

    def test_confirmation_records_coupon_redemption(self):
        """Confirming a couponed order creates a CouponRedemption row."""
        order = self._confirm_order()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(CouponRedemption.objects.filter(coupon=self.coupon).count(), 1)
        redemption = CouponRedemption.objects.get(coupon=self.coupon)
        self.assertEqual(redemption.order_id, order.pk)
        self.assertIsNone(redemption.user_id)


class OrderCancelEndpointTests(APITestCase):
    """Exercises the POST /orders/{id}/cancel/ endpoint."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=10)
        with _mock_sms():
            cart = _guest_cart("cancel-endpoint-session")
            add_item(cart, variant_id=self.variant.pk, quantity=2)
            self.order = create_order_from_cart(
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        self.url = reverse("api:orders:order-cancel", args=[self.order.lookup_token])

    def test_cancel_requires_idempotency_key(self):
        """Cancelling without an Idempotency-Key header is rejected."""
        with _mock_sms():
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_releases_stock_and_transitions(self):
        """A valid cancel releases held stock and moves the order to cancelled."""
        reservation = StockReservation.objects.get(order_item__order=self.order)
        self.assertEqual(reservation.status, "active")
        with _mock_sms():
            response = self.client.post(
                self.url, {}, HTTP_IDEMPOTENCY_KEY="cancel-1", format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, "released")

    def test_cancel_wrong_token_is_404(self):
        """A guest cancelling another person's order gets 404."""
        from uuid import uuid4

        url = reverse("api:orders:order-cancel", args=[uuid4()])
        with _mock_sms():
            response = self.client.post(
                url, {}, HTTP_IDEMPOTENCY_KEY="cancel-2", format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class StaffOrderStatusTests(APITestCase):
    """Exercises the staff-only order status-transition endpoint."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=10)
        with _mock_sms():
            cart = _guest_cart("staff-endpoint-session")
            add_item(cart, variant_id=self.variant.pk, quantity=1)
            self.order = create_order_from_cart(
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )
        self.staff = User.objects.create_user(
            email="staff@example.com",
            username="staff",
            password="StrongPass123!",
            phone_number="+254700000001",
            is_staff=True,
            role="manager",
        )
        self.url = reverse("api:orders:order-status-update", args=[self.order.pk])

    def test_unauthenticated_rejected(self):
        """An anonymous caller cannot reach the staff endpoint."""
        response = self.client.post(self.url, {"to_status": "confirmed"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_rejected(self):
        """A non-staff logged-in user is rejected with 403."""
        _make_user(email="customer@example.com", username="customer")
        _login(self.client, email="customer@example.com")
        response = self.client.post(self.url, {"to_status": "confirmed"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_without_fulfilment_role_rejected(self):
        """An is_staff user holding no fulfilment role is denied (403).

        The coarse ``is_staff`` flag alone does not grant access to the
        fulfilment endpoint — the caller must hold the manager/support role.
        """
        User.objects.create_user(
            email="nobody@example.com",
            username="nobody",
            password="StrongPass123!",
            phone_number="+254700000002",
            is_staff=True,
            role="analyst",
        )
        login_url = reverse("api:accounts:login")
        response = self.client.post(
            login_url, {"email": "nobody@example.com", "password": "StrongPass123!"}
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        response = self.client.post(self.url, {"to_status": "confirmed"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_transition_order(self):
        """A staff user can advance a pending order to confirmed."""
        login_url = reverse("api:accounts:login")
        response = self.client.post(
            login_url, {"email": "staff@example.com", "password": "StrongPass123!"}
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        response = self.client.post(
            self.url,
            {"to_status": "confirmed", "note": "verified by staff"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        self.assertTrue(
            self.order.status_history.filter(
                to_status="confirmed", changed_by=self.staff
            ).exists()
        )
        reservation = StockReservation.objects.get(order_item__order=self.order)
        self.assertEqual(reservation.status, "fulfilled")

    def test_illegal_transition_rejected(self):
        """An invalid transition (e.g. straight to delivered) returns 400."""
        login_url = reverse("api:accounts:login")
        response = self.client.post(
            login_url, {"email": "staff@example.com", "password": "StrongPass123!"}
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        response = self.client.post(self.url, {"to_status": "delivered"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PaymentMethodValidationTests(APITestCase):
    """Exercises payment-method validation at order creation."""

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        self.url = reverse("api:orders:order-list")
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=10)
        _guest_cart_for_client(self.client, self.variant, quantity=1)

    def test_disabled_payment_method_rejected(self):
        """A payment method not in the enabled set is rejected."""
        from apps.core.models import SiteConfig

        config = SiteConfig.load()
        config.settings["payment_methods"] = ["cod"]
        config.save(update_fields=["settings"])
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "mpesa"},
                HTTP_IDEMPOTENCY_KEY="pm-1",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unwired_payment_method_rejected(self):
        """An enabled-but-unwired method (card) is rejected."""
        from apps.core.models import SiteConfig

        config = SiteConfig.load()
        config.settings["payment_methods"] = ["cod", "card"]
        config.save(update_fields=["settings"])
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "card"},
                HTTP_IDEMPOTENCY_KEY="pm-2",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mpesa_unavailable_without_gateway(self):
        """An enabled M-Pesa method is rejected until a gateway is wired.

        Because no gateway is configured, an M-Pesa order must not be created:
        otherwise it would sit pending holding reserved stock with no way to
        confirm or refund it. No order or reservation is written.
        """
        from apps.core.models import SiteConfig

        config = SiteConfig.load()
        config.settings["payment_methods"] = ["mpesa", "cod"]
        config.save(update_fields=["settings"])
        with _mock_sms():
            response = self.client.post(
                self.url,
                {"phone": self.phone, "payment_method": "mpesa"},
                HTTP_IDEMPOTENCY_KEY="pm-2",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)

    def test_wired_enabled_method_accepted(self):
        """A wired, enabled payment method (cod) places the order."""
        with _mock_sms():
            response = self.client.post(
                self.url,
                {
                    "phone": self.phone,
                    "payment_method": "cod",
                    "delivery_zone_id": _active_zone().pk,
                },
                HTTP_IDEMPOTENCY_KEY="pm-3",
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class MpesaPaymentAdapterTests(APITestCase):
    """Exercises the payment-adapter completion stub contract.

    These pin the shape of the adapter so the future `payments` app fills in
    the transport without changing the order service.
    """

    def setUp(self):
        cache.clear()
        self.phone = "+254712345678"
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=10)

    def _make_order(self):
        """Create and return a pending COD order with stock reserved."""
        with _mock_sms():
            cart = _guest_cart("adapter-session")
            add_item(cart, variant_id=self.variant.pk, quantity=2)
            return create_order_from_cart(
                cart=cart,
                user=None,
                phone=self.phone,
                payment_method="cod",
                delivery_zone_id=_active_zone().pk,
            )

    def test_gateway_reports_unavailable_until_wired(self):
        """The stub gateway is not available, so M-Pesa cannot be ordered."""
        from apps.orders.payments import (
            PaymentUnavailable,
            gateway_for,
            is_payment_method_available,
        )

        self.assertFalse(is_payment_method_available("mpesa"))
        with self.assertRaises(PaymentUnavailable):
            gateway_for("mpesa").initiate(self._make_order())

    def test_confirm_handler_fulfils_stock(self):
        """The success handler confirms and fulfils the order's reservation."""
        from apps.orders.payments import confirm_order_from_payment

        order = self._make_order()
        confirm_order_from_payment(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        reservation = StockReservation.objects.get(order_item__order=order)
        self.assertEqual(reservation.status, "fulfilled")

    def test_cancel_handler_releases_stock(self):
        """The failure handler cancels and releases the order's reservation."""
        from apps.orders.payments import cancel_order_from_payment

        order = self._make_order()
        cancel_order_from_payment(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        reservation = StockReservation.objects.get(order_item__order=order)
        self.assertEqual(reservation.status, "released")
