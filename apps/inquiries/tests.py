"""Tests for the inquiries app.

Covers the public hand-off capture (anonymous allowed, shape-validated) and
the staff queue (auth + role gating, allowed transitions, conversion link).
"""

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inquiries.models import Inquiry

CREATE_URL = reverse("api:inquiries:inquiry-create")
QUEUE_URL = reverse("api:inquiries:inquiry-queue")
CART_ITEMS_URL = reverse("api:cart:cart-item-add")

SNAPSHOT = [
    {"sku": "KTL-1-V", "name": "Kettle", "quantity": 1, "price": "5000.00"},
]


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


class InquiryCreateTests(APITestCase):
    """Exercises the public capture endpoint."""

    def setUp(self):
        cache.clear()

    def test_anonymous_capture_allowed(self):
        """An anonymous visitor can log a hand-off without auth."""
        response = self.client.post(
            CREATE_URL,
            {"channel": "whatsapp", "cart_snapshot": SNAPSHOT},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Inquiry.objects.count(), 1)
        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.status, "new")
        self.assertEqual(inquiry.cart_snapshot[0]["sku"], "KTL-1-V")

    def test_capture_rejects_unknown_channel(self):
        """Only whatsapp/email channels are accepted."""
        response = self.client.post(
            CREATE_URL,
            {"channel": "sms", "cart_snapshot": SNAPSHOT},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_capture_rejects_empty_snapshot(self):
        """An empty cart snapshot carries no signal and is rejected."""
        response = self.client.post(
            CREATE_URL, {"channel": "email", "cart_snapshot": []}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_capture_rejects_missing_snapshot_without_cart(self):
        """With no server cart and no snapshot there is nothing to store."""
        response = self.client.post(CREATE_URL, {"channel": "email"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inquiry.objects.count(), 0)


class InquiryServerCartTests(APITestCase):
    """Exercises capture from the visitor's server-side cart."""

    def setUp(self):
        cache.clear()
        category, _ = Category.objects.get_or_create(
            name="Appliances", slug="appliances"
        )
        brand, _ = Brand.objects.get_or_create(name="Samsung", slug="samsung")
        product = Product.objects.create(
            name="Kettle",
            slug="kettle-inquiry",
            sku="KTL-INQ",
            description="A test product.",
            category=category,
            brand=brand,
            is_active=True,
        )
        self.variant = ProductVariant.objects.create(
            product=product,
            sku="KTL-INQ-V",
            attributes={"color": "Silver"},
            price="5000.00",
            is_active=True,
        )

    def _add_to_cart(self, quantity=2):
        """Put the variant in the visitor's server cart via the API."""
        response = self.client.post(
            CART_ITEMS_URL,
            {"variant_id": self.variant.pk, "quantity": quantity},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_capture_reads_server_cart(self):
        """The snapshot comes from the cookie-linked cart, no payload needed."""
        self._add_to_cart()
        response = self.client.post(CREATE_URL, {"channel": "whatsapp"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inquiry = Inquiry.objects.get()
        self.assertEqual(len(inquiry.cart_snapshot), 1)
        self.assertEqual(inquiry.cart_snapshot[0]["sku"], "KTL-INQ-V")
        self.assertEqual(inquiry.cart_snapshot[0]["quantity"], 2)
        self.assertEqual(inquiry.cart_snapshot[0]["price"], "5000.00")

    def test_capture_prefers_server_cart_over_client_snapshot(self):
        """A forged client snapshot cannot override the real cart lines."""
        self._add_to_cart()
        forged = [
            {"sku": "FAKE-1", "name": "Fake", "quantity": 99, "price": "1.00"},
        ]
        response = self.client.post(
            CREATE_URL,
            {"channel": "whatsapp", "cart_snapshot": forged},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.cart_snapshot[0]["sku"], "KTL-INQ-V")


class InquiryQueueTests(APITestCase):
    """Exercises staff queue access and transitions."""

    def setUp(self):
        cache.clear()
        self.inquiry = Inquiry.objects.create(
            channel="whatsapp", cart_snapshot=SNAPSHOT
        )

    def test_queue_rejects_anonymous(self):
        """Anonymous callers cannot read the staff queue."""
        response = self.client.get(QUEUE_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_queue_rejects_customer_role(self):
        """A customer credential gets no token for the staff queue."""
        _make_user("buyer@example.com", role="customer")
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)

    def test_queue_allows_support_role(self):
        """Support staff can read the queue."""
        _make_user("staff@example.com", role="support")
        _login(self.client, "staff@example.com")
        response = self.client.get(QUEUE_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_status_transition_new_to_contacted(self):
        """Staff can move a new inquiry to contacted."""
        _make_user("staff@example.com", role="support")
        _login(self.client, "staff@example.com")
        url = reverse(
            "api:inquiries:inquiry-status-update",
            kwargs={"inquiry_id": self.inquiry.pk},
        )
        response = self.client.post(url, {"to_status": "contacted"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inquiry.refresh_from_db()
        self.assertEqual(self.inquiry.status, "contacted")

    def test_status_transition_new_to_converted_rejected(self):
        """New inquiries cannot skip straight to converted via the queue."""
        _make_user("staff@example.com", role="support")
        _login(self.client, "staff@example.com")
        url = reverse(
            "api:inquiries:inquiry-status-update",
            kwargs={"inquiry_id": self.inquiry.pk},
        )
        response = self.client.post(url, {"to_status": "converted"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class InquiryReferenceTests(APITestCase):
    """Exercises the queue reference code staff match chats against."""

    def setUp(self):
        cache.clear()
        _make_user("staff@example.com", role="support")
        _login(self.client, "staff@example.com")
        self.inquiry = Inquiry.objects.create(
            channel="whatsapp",
            cart_snapshot=SNAPSHOT,
            contact_hint="+254700111222",
        )

    def test_create_response_carries_reference(self):
        """The capture response includes the code for the message text."""
        response = self.client.post(
            CREATE_URL,
            {"channel": "whatsapp", "cart_snapshot": SNAPSHOT},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Inquiry.objects.get(pk=response.data["id"])
        self.assertEqual(response.data["reference"], created.reference)
        self.assertEqual(
            Inquiry.parse_reference(response.data["reference"]), created.pk
        )

    def test_reference_format_matches_pk(self):
        """The code embeds the pk staff search by."""
        self.assertEqual(self.inquiry.reference, f"INQ-{self.inquiry.pk:06d}")
        self.assertEqual(
            Inquiry.parse_reference(self.inquiry.reference), self.inquiry.pk
        )

    def test_queue_search_by_reference(self):
        """Staff find the row by pasting the code from an incoming chat."""
        response = self.client.get(QUEUE_URL, {"search": self.inquiry.reference})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], self.inquiry.pk)

    def test_queue_search_by_contact_hint(self):
        """A phone fragment also narrows the queue."""
        response = self.client.get(QUEUE_URL, {"search": "700111222"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


class InquiryHandoffMessageTests(APITestCase):
    """Exercises the canonical pre-filled message text."""

    def setUp(self):
        cache.clear()

    def test_create_response_carries_message_text(self):
        """The capture response includes ready-to-send text with the Ref."""
        response = self.client.post(
            CREATE_URL,
            {"channel": "whatsapp", "cart_snapshot": SNAPSHOT},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Inquiry.objects.get(pk=response.data["id"])
        message = response.data["message_text"]
        self.assertIn("Kettle", message)
        self.assertIn("KTL-1-V", message)
        self.assertIn(f"Ref {created.reference}", message)
        self.assertIn("Estimated total", message)
        self.assertTrue(message.endswith(f"Ref {created.reference}"))

    def test_message_marks_unknown_prices_estimate_free(self):
        """Lines without a numeric price omit the estimated total."""
        from apps.inquiries.services import build_handoff_message

        message = build_handoff_message(
            cart_snapshot=[
                {"sku": "KTL-1", "name": "Kettle", "quantity": 2, "price": ""},
            ],
            reference="INQ-000007",
        )
        self.assertIn("price to confirm", message)
        self.assertNotIn("Estimated total", message)
        self.assertTrue(message.endswith("Ref INQ-000007"))

    def test_message_totals_line_quantities(self):
        """The estimate multiplies unit price by quantity per line."""
        from apps.inquiries.services import build_handoff_message

        message = build_handoff_message(
            cart_snapshot=[
                {"sku": "A", "name": "Kettle", "quantity": 2, "price": "5000.00"},
                {"sku": "B", "name": "Fridge", "quantity": 1, "price": "80000.00"},
            ],
            reference="INQ-000007",
        )
        self.assertIn("KES 90,000.00", message)
