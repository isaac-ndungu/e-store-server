"""Tests for the support app.

Covers the staff-only ticket lifecycle (file, reply, reopen semantics via
staff actions), the return-linked ticket flow, the staff reply/assign/status
actions, and the security matrix on every endpoint: anonymous rejection and
role-based restriction on the staff routes. Also asserts the
service-boundary invariants: message text is sanitised, ``is_staff_reply``
is server-set, and attachments are validated by content.
"""

from decimal import Decimal
from io import BytesIO

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.orders.models import Order
from apps.returns.models import ReturnRequest
from apps.support.models import Ticket, TicketMessage


def _make_user(email="buyer@example.com", username="buyer", **kwargs):
    """Create a plain customer user for tests."""
    return User.objects.create_user(
        email=email,
        username=username,
        password=kwargs.pop("password", "StrongPass123!"),
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        **kwargs,
    )


def _make_staff(email="staff@example.com", username=None, role="support"):
    """Create a staff user holding the given role."""
    return User.objects.create_user(
        email=email,
        username=username or email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254700000001",
        is_staff=True,
        role=role,
    )


def _login(client, email="buyer@example.com", password="StrongPass123!"):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_order(user):
    """Create a minimal delivered order owned by ``user``."""
    return Order.objects.create(
        user=user,
        phone="+254712345678",
        status="delivered",
        subtotal=Decimal("5000.00"),
        grand_total=Decimal("5000.00"),
    )


def _png_upload(name="photo.png"):
    """Return a small valid PNG upload for attachment tests."""
    buffer = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, format="PNG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/png")


def _post_ticket(client, url, payload, idempotency_key="ticket-key-1"):
    """POST a ticket payload with a fixed idempotency key."""
    return client.post(
        url, payload, format="json", HTTP_IDEMPOTENCY_KEY=idempotency_key
    )


class TicketAccessControlTests(APITestCase):
    """Anonymous and role access control on the staff ticket endpoints."""

    def setUp(self):
        """Create staff and a filed ticket."""
        cache.clear()
        self.staff = _make_staff()
        self.ticket = Ticket.objects.create(
            user=self.staff, category="other", subject="Broken fan"
        )

    def test_anonymous_cannot_list_or_create(self):
        """An unauthenticated caller is rejected from the ticket collection."""
        url = reverse("api:support:ticket-list-create")
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertIn(
            self.client.post(url, {}, format="json").status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_customer_credential_cannot_log_in(self):
        """A customer credential gets no token to reach tickets with."""
        _make_user()
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_list_returns_every_ticket(self):
        """The staff list is shared, not scoped to the filer."""
        other = _make_staff(email="otherstaff@example.com", role="support")
        Ticket.objects.create(user=other, category="other", subject="Other")
        _login(self.client, email="staff@example.com")
        url = reverse("api:support:ticket-list-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_anonymous_cannot_reach_staff_ticket_queue(self):
        """An unauthenticated caller cannot read the staff ticket queue."""
        url = reverse("api:support:staff-ticket-list")
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class TicketLifecycleTests(APITestCase):
    """Staff ticket filing, replies, and sanitisation."""

    def setUp(self):
        """Create staff and log in."""
        cache.clear()
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")

    def test_create_ticket_stores_first_message_and_sanitises(self):
        """Filing a ticket records the complaint with markup stripped."""
        url = reverse("api:support:ticket-list-create")
        response = _post_ticket(
            self.client,
            url,
            {
                "subject": "Fridge not cooling",
                "category": "complaint",
                "message": "It broke <script>alert(1)</script> yesterday",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "open")
        self.assertEqual(len(response.data["messages"]), 1)
        message = response.data["messages"][0]
        self.assertFalse(message["is_staff_reply"])
        self.assertNotIn("<script>", message["body"])
        self.assertIn("yesterday", message["body"])

    def test_staff_reply_on_ticket(self):
        """Staff can append a reply marked as staff."""
        ticket = Ticket.objects.create(user=self.staff, category="other", subject="Q")
        url = reverse("api:support:staff-ticket-reply", args=[ticket.pk])
        response = self.client.post(url, {"body": "We are on it"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_staff_reply"])
        self.assertEqual(ticket.messages.count(), 1)

    def test_cannot_reply_to_closed_ticket(self):
        """A closed ticket rejects further staff replies."""
        ticket = Ticket.objects.create(
            user=self.staff, category="other", subject="Q", status="closed"
        )
        url = reverse("api:support:staff-ticket-reply", args=[ticket.pk])
        response = self.client.post(url, {"body": "Reopen please"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_image_attachment_accepted(self):
        """A valid image attachment is stored on the staff reply."""
        ticket = Ticket.objects.create(user=self.staff, category="other", subject="Q")
        url = reverse("api:support:staff-ticket-reply", args=[ticket.pk])
        response = self.client.post(
            url,
            {"body": "See photo", "attachment": _png_upload()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["attachment"])

    def test_invalid_attachment_rejected(self):
        """A non-image, non-PDF attachment is rejected."""
        ticket = Ticket.objects.create(user=self.staff, category="other", subject="Q")
        bad = SimpleUploadedFile("x.txt", b"not an image", content_type="text/plain")
        url = reverse("api:support:staff-ticket-reply", args=[ticket.pk])
        response = self.client.post(
            url, {"body": "See file", "attachment": bad}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReturnLinkedTicketTests(APITestCase):
    """Filing a ticket linked to a return request."""

    def setUp(self):
        """Create staff with a delivered order and a return request."""
        cache.clear()
        self.staff = _make_staff()
        self.order = _make_order(self.staff)
        self.return_request = ReturnRequest.objects.create(
            order=self.order, reason="Damaged on arrival"
        )
        _login(self.client, email="staff@example.com")

    def test_return_linked_ticket_sets_link(self):
        """A return-linked ticket writes the link onto the return request."""
        url = reverse("api:support:ticket-list-create")
        response = _post_ticket(
            self.client,
            url,
            {
                "subject": "Refund not received",
                "category": "return",
                "message": "Please advise on my refund",
                "return_request_id": self.return_request.pk,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.ticket_id, response.data["id"])
        self.assertEqual(response.data["order"], self.order.pk)

    def test_unknown_return_rejected(self):
        """A ticket cannot link to a missing return request."""
        url = reverse("api:support:ticket-list-create")
        response = _post_ticket(
            self.client,
            url,
            {
                "subject": "Refund",
                "category": "return",
                "message": "hi",
                "return_request_id": 999999,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class StaffTicketTests(APITestCase):
    """Staff reply, assign, status, and role gating on ticket endpoints."""

    def setUp(self):
        """Create a customer ticket plus staff of various roles."""
        cache.clear()
        self.customer = _make_user()
        self.support = _make_staff(email="support@example.com", role="support")
        self.manager = _make_staff(email="manager@example.com", role="manager")
        self.analyst = _make_staff(email="analyst@example.com", role="analyst")
        self.ticket = Ticket.objects.create(
            user=self.customer, category="order_issue", subject="Where is my order"
        )

    def test_customer_cannot_reach_staff_queue(self):
        """A customer credential gets no token for the staff ticket queue."""
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)

    def test_analyst_cannot_reach_staff_queue(self):
        """An analyst role does not grant support-queue access."""
        _login(self.client, email="analyst@example.com")
        url = reverse("api:support:staff-ticket-list")
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_reply_marks_pending_and_assigns(self):
        """A staff reply moves an open ticket to pending_customer and claims it."""
        _login(self.client, email="support@example.com")
        url = reverse("api:support:staff-ticket-reply", args=[self.ticket.pk])
        response = self.client.post(
            url, {"body": "Please share your order id"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_staff_reply"])
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "pending_customer")
        self.assertEqual(self.ticket.assigned_to_id, self.support.pk)

    def test_assign_requires_support_role_target(self):
        """A ticket cannot be assigned to a non-support user."""
        _login(self.client, email="manager@example.com")
        url = reverse("api:support:staff-ticket-assign", args=[self.ticket.pk])
        response = self.client.post(url, {"agent_id": self.customer.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_transition_validated(self):
        """An illegal status transition is rejected."""
        self.ticket.status = "closed"
        self.ticket.save(update_fields=["status"])
        _login(self.client, email="manager@example.com")
        url = reverse("api:support:staff-ticket-status", args=[self.ticket.pk])
        response = self.client.post(url, {"status": "open"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resolve_ticket(self):
        """A manager can resolve an open ticket."""
        _login(self.client, email="manager@example.com")
        url = reverse("api:support:staff-ticket-status", args=[self.ticket.pk])
        response = self.client.post(url, {"status": "resolved"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "resolved")


class StaffTicketQueueFilterTests(APITestCase):
    """Query-string filtering on the staff ticket queue."""

    def setUp(self):
        """Create a ticket and log in as support staff."""
        cache.clear()
        customer = _make_user()
        _make_staff(email="support@example.com", role="support")
        Ticket.objects.create(user=customer, category="other", subject="Noise")
        _login(self.client, email="support@example.com")

    def test_non_numeric_assignee_filter_rejected(self):
        """A non-numeric assigned_to filter is a 400, not a 500."""
        url = reverse("api:support:staff-ticket-list")
        response = self.client.get(url, {"assigned_to": "abc"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_status_filter_rejected(self):
        """A status value outside the model choices is a 400."""
        url = reverse("api:support:staff-ticket-list")
        response = self.client.get(url, {"status": "nonexistent"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_assignee_filter_returns_matching_ticket(self):
        """A numeric assigned_to filter restricts the queue correctly."""
        support = _make_staff(email="assignee@example.com", role="support")
        assigned = Ticket.objects.first()
        assigned.assigned_to = support
        assigned.save(update_fields=["assigned_to"])
        url = reverse("api:support:staff-ticket-list")
        response = self.client.get(url, {"assigned_to": support.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], assigned.pk)


class TicketAttachmentTests(APITestCase):
    """Attachment storage naming, access control, and download routing."""

    def setUp(self):
        """Create staff of two roles and a ticket with a photo."""
        cache.clear()
        self.staff = _make_staff(email="filer@example.com", role="support")
        self.support = _make_staff(email="support@example.com", role="support")
        self.analyst = _make_staff(email="analyst@example.com", role="analyst")
        self.ticket = Ticket.objects.create(
            user=self.staff, category="other", subject="Broken fan"
        )
        self.attachment = _png_upload("receipt.png")
        self.message = TicketMessage.objects.create(
            ticket=self.ticket,
            sender=self.staff,
            is_staff_reply=False,
            body="See photo",
            attachment=self.attachment,
        )

    def test_attachment_stored_under_random_name(self):
        """The stored filename carries no client-supplied name."""
        stored = self.message.attachment.name
        self.assertTrue(stored.startswith("support/attachments/"))
        self.assertNotIn("receipt.png", stored)

    def test_customer_token_cannot_download_attachment(self):
        """A customer token is refused from the download route."""
        _make_user()
        self.client.force_authenticate(user=User.objects.get(email="buyer@example.com"))
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_download_attachment(self):
        """An unauthenticated caller is rejected from the download route."""
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_support_staff_can_download_attachment(self):
        """Support staff can fetch any ticket's attachment."""
        _login(self.client, email="support@example.com")
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_non_support_role_cannot_download_attachment(self):
        """An analyst role resolves to 403 on the download route."""
        _login(self.client, email="analyst@example.com")
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_message_without_attachment_resolves_to_404(self):
        """A message carrying no file yields 404, never an empty download."""
        plain = TicketMessage.objects.create(
            ticket=self.ticket, sender=self.staff, body="no file"
        )
        _login(self.client, email="support@example.com")
        url = reverse("api:support:ticket-attachment-download", args=[plain.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_serializer_exposes_download_url(self):
        """The staff shape uses the protected download route."""
        _login(self.client, email="support@example.com")
        url = reverse("api:support:staff-ticket-detail", args=[self.ticket.pk])
        response = self.client.get(url)
        messages_with_files = [m for m in response.data["messages"] if m["attachment"]]
        self.assertTrue(messages_with_files)
        self.assertIn("/support/attachments/", messages_with_files[0]["attachment"])


class TicketIdempotencyTests(APITestCase):
    """Idempotency-key enforcement on staff ticket filing."""

    def setUp(self):
        """Create staff and log in."""
        cache.clear()
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")

    def test_ticket_create_requires_idempotency_key(self):
        """Ticket creation without an Idempotency-Key is a 400."""
        url = reverse("api:support:ticket-list-create")
        response = self.client.post(
            url,
            {"subject": "Q", "category": "other", "message": "hi"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_repeated_idempotency_key_creates_single_ticket(self):
        """Repeating a successful key replays the response, not a new ticket."""
        url = reverse("api:support:ticket-list-create")
        payload = {"subject": "Q", "category": "other", "message": "hi"}
        first = _post_ticket(self.client, url, payload, idempotency_key="dup-key")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = _post_ticket(self.client, url, payload, idempotency_key="dup-key")
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(Ticket.objects.filter(user=self.staff).count(), 1)

    def test_distinct_idempotency_keys_create_distinct_tickets(self):
        """Different keys for the same payload are treated as separate tickets."""
        url = reverse("api:support:ticket-list-create")
        payload = {"subject": "Q", "category": "other", "message": "hi"}
        first = _post_ticket(self.client, url, payload, idempotency_key="key-a")
        second = _post_ticket(self.client, url, payload, idempotency_key="key-b")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(second.data["id"], first.data["id"])
        self.assertEqual(Ticket.objects.filter(user=self.staff).count(), 2)
