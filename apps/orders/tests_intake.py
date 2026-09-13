"""Tests for the staff order-intake endpoint.

Covers the assisted-sale path: staff-only access (anonymous and customer
tokens rejected), server-side repricing, the staff-quoted delivery fee,
confirmed creation with a status-history row, inquiry conversion linking,
idempotent retries, and out-of-stock rejection. No stock is touched anywhere
— availability is a staff-set flag.
"""

from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inquiries.models import Inquiry
from apps.orders.models import Order
from apps.shipping.models import DeliveryArea

INTAKE_URL = reverse("api:orders:order-intake")

_SEQ = [0]


def _make_user(email, role="customer"):
    """Create a user with the given role."""
    return User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254712345678",
        role=role,
    )


def _login(client, email):
    """Attach a JWT for the user to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(
        url, {"email": email, "password": "StrongPass123!"}, format="json"
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_variant(price="5000.00", stock_status="in_stock"):
    """Create an active variant with no stock rows behind it."""
    _SEQ[0] += 1
    n = _SEQ[0]
    category, _ = Category.objects.get_or_create(name="Appliances", slug="appliances")
    brand, _ = Brand.objects.get_or_create(name="Samsung", slug="samsung")
    product = Product.objects.create(
        name=f"Kettle {n}",
        slug=f"kettle-{n}",
        sku=f"KTL-{n}",
        description="A test product.",
        category=category,
        brand=brand,
        is_active=True,
    )
    return ProductVariant.objects.create(
        product=product,
        sku=f"KTL-{n}-V",
        attributes={"color": "Silver"},
        price=price,
        package_weight=Decimal("2.00"),
        stock_status=stock_status,
        is_active=True,
    )


def _make_area():
    """Create an active delivery area."""
    return DeliveryArea.objects.create(
        county="Nairobi", area_name="Westlands", is_active=True
    )


def _intake_payload(variant, **overrides):
    """Build a minimal intake payload for the variant."""
    payload = {
        "phone": "+254712345678",
        "order_source": "whatsapp",
        "payment_method": "cod",
        "items": [{"variant_id": variant.pk, "quantity": 2}],
    }
    payload.update(overrides)
    return payload


class StaffIntakeAccessTests(APITestCase):
    """Exercises auth gating on the intake endpoint."""

    def setUp(self):
        cache.clear()

    def test_intake_rejects_anonymous(self):
        """Anonymous callers cannot create staff orders."""
        variant = _make_variant()
        response = self.client.post(
            INTAKE_URL,
            _intake_payload(variant),
            format="json",
            HTTP_IDEMPOTENCY_KEY="k1",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_intake_rejects_customer_role(self):
        """A customer credential cannot log in, so no token ever reaches intake."""
        _make_user("buyer@example.com", role="customer")
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)


class StaffIntakeTests(APITestCase):
    """Exercises the happy path and money rules."""

    def setUp(self):
        cache.clear()
        _make_user("staff@example.com", role="support")
        _login(self.client, "staff@example.com")

    def _post(self, payload, key="intake-1"):
        """POST the payload with an idempotency key."""
        return self.client.post(
            INTAKE_URL, payload, format="json", HTTP_IDEMPOTENCY_KEY=key
        )

    def test_intake_creates_confirmed_order_with_history(self):
        """Staff intake creates a confirmed order without touching stock."""
        variant = _make_variant(price="5000.00")
        response = self._post(
            _intake_payload(
                variant, payment_reference="QHX7ABC123", order_source="whatsapp"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.order_source, "whatsapp")
        self.assertEqual(order.payment_reference, "QHX7ABC123")
        self.assertIsNotNone(order.staff_created_by)
        self.assertEqual(order.status_history.count(), 1)
        self.assertEqual(order.delivery_fee, Decimal("0.00"))

    def test_intake_reprices_server_side(self):
        """The charged unit price comes from the catalogue, not the client."""
        variant = _make_variant(price="5000.00")
        payload = _intake_payload(variant)
        payload["items"][0]["unit_price"] = "1.00"
        response = self._post(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(pk=response.data["id"])
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("5000.00"))

    def test_intake_stores_quoted_delivery_fee_with_vat(self):
        """The staff-quoted fee is stored and taxed like before."""
        variant = _make_variant(price="5000.00")
        area = _make_area()
        response = self._post(
            _intake_payload(variant, delivery_area_id=area.pk, delivery_fee="450.00"),
            key="fee-1",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.delivery_fee, Decimal("450.00"))
        self.assertEqual(order.delivery_area, area)
        self.assertEqual(order.shipping_tax_amount, Decimal("72.00"))
        self.assertEqual(
            order.grand_total,
            Decimal("10000.00")
            + Decimal("450.00")
            + Decimal("72.00")
            + order.tax_total,
        )

    def test_intake_rejects_negative_delivery_fee(self):
        """A negative quoted fee fails validation and creates nothing."""
        variant = _make_variant(price="5000.00")
        response = self._post(
            _intake_payload(variant, delivery_fee="-10.00"), key="fee-neg"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)

    def test_intake_rejects_unknown_delivery_area(self):
        """An unknown area id fails validation and creates nothing."""
        variant = _make_variant(price="5000.00")
        response = self._post(
            _intake_payload(variant, delivery_area_id=999999), key="area-bad"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)

    def test_intake_rejects_out_of_stock_variant(self):
        """A variant staff flagged out of stock cannot be ordered."""
        variant = _make_variant(price="5000.00", stock_status="out_of_stock")
        response = self._post(_intake_payload(variant), key="oos-1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)

    def test_intake_converts_inquiry(self):
        """Passing inquiry_id links the order and converts the inquiry."""
        variant = _make_variant(price="5000.00")
        inquiry = Inquiry.objects.create(
            channel="whatsapp",
            cart_snapshot=[
                {
                    "sku": variant.sku,
                    "name": "Kettle",
                    "quantity": 2,
                    "price": "5000.00",
                }
            ],
        )
        payload = _intake_payload(variant, inquiry_id=inquiry.pk)
        response = self._post(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, "converted")
        self.assertEqual(inquiry.converted_order_id, response.data["id"])

    def test_intake_idempotent_retry(self):
        """Repeating the same idempotency key returns the first order."""
        variant = _make_variant(price="5000.00")
        payload = _intake_payload(variant)
        first = self._post(payload, key="same-key")
        second = self._post(payload, key="same-key")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_intake_key_reused_with_different_payload_conflicts(self):
        """The same key with a different body is a 409, not a replay."""
        variant = _make_variant(price="5000.00")
        payload = _intake_payload(variant)
        first = self._post(payload, key="reused-key")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        payload["items"][0]["quantity"] = 3
        second = self._post(payload, key="reused-key")
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Order.objects.count(), 1)

    def test_intake_warns_on_duplicate_payment_reference(self):
        """Reusing a receipt code still creates the order, with a warning."""
        variant = _make_variant(price="5000.00")
        first = self._post(
            _intake_payload(variant, payment_reference="DUPREF123"), key="dup-1"
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("warnings", first.data)
        second = self._post(
            _intake_payload(variant, payment_reference="DUPREF123"), key="dup-2"
        )
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(len(second.data["warnings"]), 1)
        warning = second.data["warnings"][0]
        self.assertEqual(warning["code"], "duplicate_payment_reference")
        self.assertEqual(warning["order_id"], first.data["id"])

    def test_intake_no_warning_for_blank_reference(self):
        """Orders without a reference never warn."""
        variant = _make_variant(price="5000.00")
        first = self._post(_intake_payload(variant), key="blank-1")
        second = self._post(_intake_payload(variant), key="blank-2")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("warnings", second.data)
