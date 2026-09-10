"""Tests for the staff order-intake endpoint.

Covers the assisted-sale path: staff-only access (anonymous and customer
tokens rejected), server-side repricing, direct stock deduction with a
status-history row, inquiry conversion linking, idempotent retries, and
insufficient-stock rejection.
"""

from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inquiries.models import Inquiry
from apps.inventory.models import Inventory, Warehouse
from apps.orders.models import Order

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


def _make_stocked_variant(price="5000.00", quantity=10):
    """Create an active variant with real inventory rows."""
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
    variant = ProductVariant.objects.create(
        product=product,
        sku=f"KTL-{n}-V",
        attributes={"color": "Silver"},
        price=price,
        package_weight=Decimal("2.00"),
        is_active=True,
    )
    warehouse, _ = Warehouse.objects.get_or_create(name="Main")
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity, "reserved": 0},
    )
    return variant


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
        variant = _make_stocked_variant()
        response = self.client.post(
            INTAKE_URL,
            _intake_payload(variant),
            format="json",
            HTTP_IDEMPOTENCY_KEY="k1",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_intake_rejects_customer_role(self):
        """A customer token cannot reach the staff endpoint."""
        variant = _make_stocked_variant()
        _make_user("buyer@example.com", role="customer")
        _login(self.client, "buyer@example.com")
        response = self.client.post(
            INTAKE_URL,
            _intake_payload(variant),
            format="json",
            HTTP_IDEMPOTENCY_KEY="k1",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class StaffIntakeTests(APITestCase):
    """Exercises the happy path and stock/money rules."""

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
        """Staff intake creates a confirmed order and deducts stock."""
        variant = _make_stocked_variant(price="5000.00", quantity=10)
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
        inventory = Inventory.objects.get(variant=variant)
        self.assertEqual(inventory.quantity, 8)

    def test_intake_reprices_server_side(self):
        """The charged unit price comes from the catalogue, not the client."""
        variant = _make_stocked_variant(price="5000.00", quantity=10)
        payload = _intake_payload(variant)
        payload["items"][0]["unit_price"] = "1.00"
        response = self._post(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get(pk=response.data["id"])
        item = order.items.get()
        self.assertEqual(item.unit_price, Decimal("5000.00"))

    def test_intake_rejects_insufficient_stock(self):
        """Ordering more than available fails without creating an order."""
        variant = _make_stocked_variant(price="5000.00", quantity=1)
        payload = _intake_payload(variant)
        payload["items"][0]["quantity"] = 5
        response = self._post(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)

    def test_intake_converts_inquiry(self):
        """Passing inquiry_id links the order and converts the inquiry."""
        variant = _make_stocked_variant(price="5000.00", quantity=10)
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
        variant = _make_stocked_variant(price="5000.00", quantity=10)
        payload = _intake_payload(variant)
        first = self._post(payload, key="same-key")
        second = self._post(payload, key="same-key")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 1)
