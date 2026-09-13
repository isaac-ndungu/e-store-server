"""Tests for the orders app.

Covers staff order intake (confirmed creation, server-side pricing,
order-time snapshots, the staff-quoted delivery fee, status-history audit),
staff-driven status transitions, fulfilment completion to delivered, and
payment-method validation. Endpoint coverage for intake idempotency and
access control lives in ``tests_intake.py``.
"""

from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.orders.models import Order
from apps.orders.services import apply_staff_status, create_staff_order

_SEQ = [0]


def _make_staff(email="staff@example.com", role="support"):
    """Create a staff user for intake tests."""
    return User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254700000001",
        role=role,
    )


def _login(client, email="staff@example.com"):
    """Attach a JWT for the staff user to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(
        url, {"email": email, "password": "StrongPass123!"}, format="json"
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_product(price="5000.00", stock_status="in_stock", **kwargs):
    """Create an active product with a default active variant."""
    _SEQ[0] += 1
    n = _SEQ[0]
    category = Category.objects.get_or_create(name="Appliances", slug="appliances")[0]
    brand = Brand.objects.get_or_create(name="Samsung", slug="samsung")[0]
    product = Product.objects.create(
        name=f"Kettle {n}",
        slug=f"kettle-{n}",
        sku=f"KTL-{n}",
        description="A test product.",
        category=category,
        brand=brand,
        is_active=True,
        **kwargs,
    )
    variant = ProductVariant.objects.create(
        product=product,
        sku=f"KTL-{n}-V",
        attributes={"color": "Silver"},
        price=price,
        package_weight="2.00",
        stock_status=stock_status,
        is_active=True,
    )
    return product, variant


def _place_intake_order(variant, staff=None, quantity=1, **kwargs):
    """Create a confirmed intake order for a variant."""
    staff = staff or _make_staff(email=f"s{_SEQ[0]}@example.com")
    params = {
        "staff_user": staff,
        "phone": "+254712345678",
        "lines": [{"variant_id": variant.pk, "quantity": quantity}],
        "order_source": "whatsapp",
        "payment_method": "cod",
    }
    params.update(kwargs)
    return create_staff_order(**params)


class IntakeOrderTests(APITestCase):
    """Exercises staff order creation through the service layer."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        _, self.variant = _make_product()

    def test_intake_creates_confirmed_order_with_history(self):
        """A staff intake lands confirmed with a single audit row."""
        order = _place_intake_order(self.variant, staff=self.staff)
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.staff_created_by, self.staff)
        self.assertEqual(order.status_history.count(), 1)
        history = order.status_history.first()
        self.assertEqual(history.from_status, "")
        self.assertEqual(history.to_status, "confirmed")

    def test_intake_snapshots_product_data(self):
        """Order items snapshot catalogue data at creation time."""
        _, variant = _make_product(price="3000.00")
        order = _place_intake_order(variant, staff=self.staff, quantity=2)
        item = order.items.get()
        self.assertEqual(item.product_name, variant.product.name)
        self.assertEqual(item.variant_sku, variant.sku)
        self.assertEqual(item.unit_price, Decimal("3000.00"))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.total_price, Decimal("6000.00"))

    def test_intake_applies_active_promotion(self):
        """The charged unit price reflects the current effective price."""
        _, variant = _make_product(price="5000.00")
        from datetime import timedelta

        from django.utils import timezone

        from apps.promotions.services import create_discount

        create_discount(
            name="Spring Sale",
            scope="variant",
            discount_type="percent",
            value="10.00",
            starts_at=timezone.now() - timedelta(days=1),
            variants=[variant.pk],
        )
        order = _place_intake_order(variant, staff=self.staff, quantity=2)
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("4500.00"))

    def test_intake_needs_no_stock_rows(self):
        """Creation succeeds with no stock tracking behind the variant."""
        order = _place_intake_order(self.variant, staff=self.staff, quantity=3)
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.items.get().quantity, 3)

    def test_intake_rejects_out_of_stock_variant(self):
        """A variant flagged out of stock fails and creates nothing."""
        _, variant = _make_product(stock_status="out_of_stock")
        with self.assertRaises(ValidationError):
            _place_intake_order(variant, staff=self.staff, quantity=1)
        self.assertEqual(Order.objects.count(), 0)

    def test_intake_records_quoted_delivery_fee(self):
        """The staff-quoted fee is stored with VAT on top."""
        order = _place_intake_order(
            self.variant, staff=self.staff, delivery_fee=Decimal("450.00")
        )
        self.assertEqual(order.delivery_fee, Decimal("450.00"))
        self.assertEqual(order.shipping_tax_amount, Decimal("72.00"))

    def test_intake_rejects_negative_delivery_fee(self):
        """A negative fee fails and creates nothing."""
        with self.assertRaises(ValidationError):
            _place_intake_order(
                self.variant, staff=self.staff, delivery_fee=Decimal("-5.00")
            )
        self.assertEqual(Order.objects.count(), 0)

    def test_intake_rejects_disabled_payment_method(self):
        """A method outside the enabled list is rejected."""
        with self.assertRaises(ValidationError):
            _place_intake_order(self.variant, staff=self.staff, payment_method="card")

    def test_intake_accepts_bank_transfer(self):
        """Bank transfers are a first-class assisted payment method."""
        order = _place_intake_order(
            self.variant,
            staff=self.staff,
            payment_method="bank_transfer",
            payment_reference="TRX2026ABC",
        )
        self.assertEqual(order.payment_method, "bank_transfer")
        self.assertEqual(order.payment_reference, "TRX2026ABC")

    def test_intake_rejects_unknown_source(self):
        """An order source outside the choices is rejected."""
        with self.assertRaises(ValidationError):
            _place_intake_order(self.variant, staff=self.staff, order_source="website")


class StaffOrderStatusTests(APITestCase):
    """Exercises staff-driven status transitions over HTTP."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        _, self.variant = _make_product()
        self.order = _place_intake_order(self.variant, staff=self.staff)
        self.url = reverse(
            "api:orders:order-status-update", kwargs={"order_id": self.order.pk}
        )

    def test_anonymous_status_update_rejected(self):
        """Anonymous callers cannot move an order."""
        response = self.client.post(self.url, {"to_status": "processing"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_status_update_rejected(self):
        """A customer token cannot reach the staff endpoint."""
        User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
            role="customer",
        )
        url = reverse("api:accounts:login")
        login = self.client.post(
            url,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_advances_status_with_history(self):
        """A legal transition moves the order and appends history."""
        _login(self.client)
        response = self.client.post(
            self.url, {"to_status": "processing", "note": "Packed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "processing")
        self.assertEqual(self.order.status_history.count(), 2)

    def test_staff_completes_order_through_to_delivered(self):
        """The fulfilment pipeline runs confirmed to delivered."""
        _login(self.client)
        for target in ("processing", "shipped", "delivered"):
            response = self.client.post(self.url, {"to_status": target})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "delivered")
        self.assertEqual(self.order.status_history.count(), 4)

    def test_illegal_transition_rejected(self):
        """Skipping the fulfilment graph fails cleanly."""
        _login(self.client)
        response = self.client.post(self.url, {"to_status": "delivered"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_via_status_endpoint_rejected(self):
        """Cancellation must go through the cancel flow, not here."""
        _login(self.client)
        response = self.client.post(self.url, {"to_status": "cancelled"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_missing_order_is_404(self):
        """An unknown order id is a 404."""
        _login(self.client)
        url = reverse("api:orders:order-status-update", kwargs={"order_id": 999999})
        response = self.client.post(url, {"to_status": "processing"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PaymentMethodChoicesTests(APITestCase):
    """Locks the assisted payment-method set."""

    def test_retired_methods_are_gone(self):
        """Card and invoice are removed; bank transfer is offered."""
        keys = dict(Order.PAYMENT_METHOD_CHOICES)
        self.assertNotIn("card", keys)
        self.assertNotIn("invoice", keys)
        self.assertIn("bank_transfer", keys)
        self.assertIn("mpesa", keys)
        self.assertIn("cod", keys)

    def test_apply_staff_status_rejects_bad_transition(self):
        """The service guards the transition graph directly."""
        cache.clear()
        staff = _make_staff(email="svc@example.com")
        _, variant = _make_product()
        order = _place_intake_order(variant, staff=staff)
        with self.assertRaises(ValidationError):
            apply_staff_status(order, "delivered", changed_by=staff)

    def test_apply_staff_status_rejects_terminal_states(self):
        """Cancel/refund/return must go through the cancel/return flow."""
        cache.clear()
        staff = _make_staff(email="money@example.com")
        _, variant = _make_product()
        order = _place_intake_order(variant, staff=staff)
        for target in ("cancelled", "refunded", "returned"):
            with self.assertRaises(ValidationError):
                apply_staff_status(order, target, changed_by=staff)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
