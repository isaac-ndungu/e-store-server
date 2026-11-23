"""Tests for the inquiries app.

Covers the public hand-off capture (anonymous allowed, shape-validated) and
the staff queue (auth + role gating, allowed transitions, conversion link).
"""

from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product
from apps.inquiries.models import Inquiry

CREATE_URL = reverse("api:inquiries:inquiry-create")
QUEUE_URL = reverse("api:inquiries:inquiry-queue")
CART_ITEMS_URL = reverse("api:cart:cart-item-add")

SNAPSHOT = [
    {"sku": "KTL-1-V", "name": "Kettle", "quantity": 1, "price": "5000.00"},
]


def _make_user(email, is_staff=False):
    """Create a user for tests."""
    return User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254712345678",
        is_staff=is_staff,
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

    def test_capture_stores_normalized_contact_facts(self):
        """Structured contact fields are stored in one canonical shape."""
        response = self.client.post(
            CREATE_URL,
            {
                "channel": "email",
                "cart_snapshot": SNAPSHOT,
                "customer_name": "  Amina Yusuf ",
                "customer_phone": "0712 345 678",
                "customer_email": " AMINA@Example.com ",
                "delivery_location": " Kileleshwa, Nairobi ",
                "order_notes": " Call on arrival ",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.customer_name, "Amina Yusuf")
        self.assertEqual(inquiry.customer_phone, "+254712345678")
        self.assertEqual(inquiry.customer_email, "amina@example.com")
        self.assertEqual(inquiry.delivery_location, "Kileleshwa, Nairobi")
        self.assertEqual(inquiry.order_notes, "Call on arrival")
        self.assertEqual(
            inquiry.contact_hint,
            "Amina Yusuf | +254712345678 | Kileleshwa, Nairobi | "
            "amina@example.com | Call on arrival",
        )
        self.assertEqual(response.data["customer_name"], "Amina Yusuf")
        self.assertEqual(response.data["customer_phone"], "+254712345678")

    def test_capture_accepts_all_supported_phone_shapes(self):
        """Leading zero, country code, and bare nine-digit input normalize once."""
        for submitted, expected in (
            ("0712345678", "+254712345678"),
            ("0112345678", "+254112345678"),
            ("+254712345678", "+254712345678"),
            ("254712345678", "+254712345678"),
            ("712345678", "+254712345678"),
        ):
            response = self.client.post(
                CREATE_URL,
                {
                    "channel": "whatsapp",
                    "cart_snapshot": SNAPSHOT,
                    "customer_phone": submitted,
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data["customer_phone"], expected)

    def test_capture_rejects_invalid_or_non_kenyan_phone(self):
        """A number that cannot resolve to a Kenyan mobile yields HTTP 400."""
        for submitted in ("123", "not-a-phone", "+255712345678", "+254201234567"):
            response = self.client.post(
                CREATE_URL,
                {
                    "channel": "whatsapp",
                    "cart_snapshot": SNAPSHOT,
                    "customer_phone": submitted,
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_honeypot_submission_is_rejected_without_a_row(self):
        """The hidden field catches obvious bots before anything is stored."""
        response = self.client.post(
            CREATE_URL,
            {
                "channel": "whatsapp",
                "cart_snapshot": SNAPSHOT,
                "company_website": "https://spam.example",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_submission_faster_than_minimum_interval_is_rejected(self):
        """A form timestamped now is treated as automated traffic."""
        response = self.client.post(
            CREATE_URL,
            {
                "channel": "whatsapp",
                "cart_snapshot": SNAPSHOT,
                "form_started_at": timezone.now().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_submission_after_minimum_interval_is_accepted(self):
        """A realistic form fill interval passes the timing check."""
        response = self.client.post(
            CREATE_URL,
            {
                "channel": "whatsapp",
                "cart_snapshot": SNAPSHOT,
                "form_started_at": (timezone.now() - timedelta(seconds=5)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_future_form_timestamp_is_not_rejected_for_clock_skew(self):
        """A device clock running ahead must not look like a bot."""
        response = self.client.post(
            CREATE_URL,
            {
                "channel": "whatsapp",
                "cart_snapshot": SNAPSHOT,
                "form_started_at": (timezone.now() + timedelta(seconds=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_generated_contact_hint_fits_the_legacy_column(self):
        """Long structured notes never overflow the 255-character summary."""
        response = self.client.post(
            CREATE_URL,
            {
                "channel": "email",
                "cart_snapshot": SNAPSHOT,
                "customer_name": "Amina",
                "order_notes": "x" * 300,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertLessEqual(len(Inquiry.objects.get().contact_hint), 255)

    def test_idempotency_key_replays_the_first_inquiry(self):
        """A repeated submit with the same key creates one row and one reference."""
        payload = {
            "channel": "email",
            "cart_snapshot": SNAPSHOT,
            "customer_name": "Amina Yusuf",
            "customer_phone": "0712345678",
        }
        headers = {"HTTP_IDEMPOTENCY_KEY": "inquiry-test-key"}
        first = self.client.post(CREATE_URL, payload, format="json", **headers)
        second = self.client.post(CREATE_URL, payload, format="json", **headers)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["reference"], second.data["reference"])
        self.assertEqual(Inquiry.objects.count(), 1)

    def test_idempotency_key_rejects_a_different_payload(self):
        """Reusing a key for different details yields 409, not a wrong replay."""
        headers = {"HTTP_IDEMPOTENCY_KEY": "inquiry-conflict-key"}
        first = self.client.post(
            CREATE_URL,
            {"channel": "whatsapp", "cart_snapshot": SNAPSHOT},
            format="json",
            **headers,
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        conflict = self.client.post(
            CREATE_URL,
            {
                "channel": "whatsapp",
                "cart_snapshot": SNAPSHOT,
                "customer_name": "Different",
            },
            format="json",
            **headers,
        )
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Inquiry.objects.count(), 1)


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
            price="5000.00",
        )
        self.product = product

    def _add_to_cart(self, quantity=2):
        """Put the product in the visitor's server cart via the API."""
        response = self.client.post(
            CART_ITEMS_URL,
            {"product_id": self.product.pk, "quantity": quantity},
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
        self.assertEqual(inquiry.cart_snapshot[0]["sku"], "KTL-INQ")
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
        self.assertEqual(inquiry.cart_snapshot[0]["sku"], "KTL-INQ")


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
        """A non-staff credential gets no token for the staff queue."""
        _make_user("buyer@example.com", is_staff=False)
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)

    def test_queue_allows_support_role(self):
        """Staff can read the queue."""
        _make_user("staff@example.com", is_staff=True)
        _login(self.client, "staff@example.com")
        response = self.client.get(QUEUE_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_status_transition_new_to_contacted(self):
        """Staff can move a new inquiry to contacted."""
        _make_user("staff@example.com", is_staff=True)
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
        _make_user("staff@example.com", is_staff=True)
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
        _make_user("staff@example.com", is_staff=True)
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

    def test_queue_search_by_structured_contact(self):
        """Staff can search the structured name and phone fields too."""
        Inquiry.objects.create(
            channel="email",
            cart_snapshot=SNAPSHOT,
            customer_name="Wanjiku Kamau",
            customer_phone="+254733111222",
            customer_email="wanjiku@example.com",
        )
        for search in ("Wanjiku", "733111222", "wanjiku@example.com"):
            response = self.client.get(QUEUE_URL, {"search": search})
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


class OrderIntakeEmailBodyTests(APITestCase):
    """Exercises the staff email body built from an inquiry's contact facts."""

    def test_body_uses_structured_contact_fields(self):
        """Each structured contact fact is labelled in the staff email."""
        from apps.inquiries.email import _order_intake_body

        inquiry = Inquiry.objects.create(
            channel="email",
            cart_snapshot=SNAPSHOT,
            contact_hint="legacy | hint",
            customer_name="Amina Yusuf",
            customer_phone="+254712345678",
            customer_email="amina@example.com",
            delivery_location="Kileleshwa",
            order_notes="Call on arrival",
        )

        body = _order_intake_body(inquiry)

        self.assertIn("Name: Amina Yusuf", body)
        self.assertIn("Phone: +254712345678", body)
        self.assertIn("Email: amina@example.com", body)
        self.assertIn("Delivery location: Kileleshwa", body)
        self.assertIn("Order notes: Call on arrival", body)
        self.assertNotIn("Customer contact:", body)

    def test_body_keeps_the_legacy_hint_when_structured_fields_are_blank(self):
        """Older inquiries still show their single contact hint."""
        from apps.inquiries.email import _order_intake_body

        inquiry = Inquiry.objects.create(
            channel="email",
            cart_snapshot=SNAPSHOT,
            contact_hint="buyer@example.com",
        )

        body = _order_intake_body(inquiry)

        self.assertIn("Customer contact: buyer@example.com", body)
