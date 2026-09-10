"""Tests for the inquiries app.

Covers the public hand-off capture (anonymous allowed, shape-validated) and
the staff queue (auth + role gating, allowed transitions, conversion link).
"""

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.inquiries.models import Inquiry

CREATE_URL = reverse("api:inquiries:inquiry-create")
QUEUE_URL = reverse("api:inquiries:inquiry-queue")

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
        """A customer token cannot read the staff queue."""
        _make_user("buyer@example.com", role="customer")
        _login(self.client, "buyer@example.com")
        response = self.client.get(QUEUE_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
