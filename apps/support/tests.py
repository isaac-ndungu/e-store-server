"""Tests for the support app.

Covers the customer ticket lifecycle (open, message, reopen), the return-linked
ticket flow, the staff reply/assign/status actions, live chat for both a
logged-in customer and a guest, and the security matrix on every endpoint:
anonymous rejection, cross-customer isolation (404, never another's data), and
role-based restriction on the staff routes. Also asserts the service-boundary
invariants: message text is sanitised, ``is_staff_reply``/``sender_type`` are
server-set, and attachments are validated by content.
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
from apps.support.models import ChatMessage, ChatSession, Ticket, TicketMessage


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
    """Anonymous and cross-customer access control on ticket endpoints."""

    def setUp(self):
        """Create two customers and a ticket owned by the first."""
        cache.clear()
        self.owner = _make_user()
        self.other = _make_user(email="other@example.com", username="other")
        self.ticket = Ticket.objects.create(
            user=self.owner, category="other", subject="Broken fan"
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

    def test_other_customer_gets_404_on_foreign_ticket(self):
        """A different customer cannot read someone else's ticket."""
        _login(self.client, email="other@example.com")
        url = reverse("api:support:ticket-detail", args=[self.ticket.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_list_returns_only_own_tickets(self):
        """A customer's list contains only their own tickets."""
        Ticket.objects.create(user=self.other, category="other", subject="Other")
        _login(self.client)
        url = reverse("api:support:ticket-list-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], self.ticket.pk)


class TicketLifecycleTests(APITestCase):
    """Ticket creation, message exchange, reopen, and sanitisation."""

    def setUp(self):
        """Create a customer and log in."""
        cache.clear()
        self.user = _make_user()
        _login(self.client)

    def test_create_ticket_stores_first_message_and_sanitises(self):
        """Opening a ticket records the opening message with markup stripped."""
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

    def test_customer_message_on_own_ticket(self):
        """A customer can append a message to their own ticket."""
        ticket = Ticket.objects.create(user=self.user, category="other", subject="Q")
        url = reverse("api:support:ticket-message-create", args=[ticket.pk])
        response = self.client.post(url, {"body": "Any update?"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data["is_staff_reply"])
        self.assertEqual(ticket.messages.count(), 1)

    def test_customer_reply_reopens_pending_ticket(self):
        """A customer reply moves a pending_customer ticket back to open."""
        ticket = Ticket.objects.create(
            user=self.user,
            category="other",
            subject="Q",
            status="pending_customer",
        )
        url = reverse("api:support:ticket-message-create", args=[ticket.pk])
        self.client.post(url, {"body": "Here is the info"}, format="json")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "open")

    def test_cannot_message_closed_ticket(self):
        """A closed ticket rejects further customer messages."""
        ticket = Ticket.objects.create(
            user=self.user, category="other", subject="Q", status="closed"
        )
        url = reverse("api:support:ticket-message-create", args=[ticket.pk])
        response = self.client.post(url, {"body": "Reopen please"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_image_attachment_accepted(self):
        """A valid image attachment is stored on the message."""
        ticket = Ticket.objects.create(user=self.user, category="other", subject="Q")
        url = reverse("api:support:ticket-message-create", args=[ticket.pk])
        response = self.client.post(
            url,
            {"body": "See photo", "attachment": _png_upload()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["attachment"])

    def test_invalid_attachment_rejected(self):
        """A non-image, non-PDF attachment is rejected."""
        ticket = Ticket.objects.create(user=self.user, category="other", subject="Q")
        bad = SimpleUploadedFile("x.txt", b"not an image", content_type="text/plain")
        url = reverse("api:support:ticket-message-create", args=[ticket.pk])
        response = self.client.post(
            url, {"body": "See file", "attachment": bad}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReturnLinkedTicketTests(APITestCase):
    """Opening a ticket linked to a return request."""

    def setUp(self):
        """Create a customer with a delivered order and a return request."""
        cache.clear()
        self.user = _make_user()
        self.other = _make_user(email="other@example.com", username="other")
        self.order = _make_order(self.user)
        self.return_request = ReturnRequest.objects.create(
            order=self.order, reason="Damaged on arrival"
        )
        _login(self.client)

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

    def test_cannot_link_foreign_return(self):
        """A customer cannot link a ticket to another customer's return."""
        foreign_order = _make_order(self.other)
        foreign_return = ReturnRequest.objects.create(order=foreign_order, reason="x")
        url = reverse("api:support:ticket-list-create")
        response = _post_ticket(
            self.client,
            url,
            {
                "subject": "Refund",
                "category": "return",
                "message": "hi",
                "return_request_id": foreign_return.pk,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        foreign_return.refresh_from_db()
        self.assertIsNone(foreign_return.ticket_id)


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
        """A customer token is forbidden from the staff ticket queue."""
        _login(self.client)
        url = reverse("api:support:staff-ticket-list")
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

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

    def test_customer_detail_hides_staff_identity(self):
        """The customer detail shape never names the responding staff member."""
        TicketMessage.objects.create(
            ticket=self.ticket,
            sender=self.support,
            is_staff_reply=True,
            body="hello",
        )
        _login(self.client)
        url = reverse("api:support:ticket-detail", args=[self.ticket.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for message in response.data["messages"]:
            self.assertNotIn("sender", message)


class ChatTests(APITestCase):
    """Live-chat flows for logged-in customers and guests, plus staff access."""

    def setUp(self):
        """Create a customer and a support agent."""
        cache.clear()
        self.user = _make_user()
        self.support = _make_staff(email="support@example.com", role="support")

    def test_guest_chat_flow(self):
        """A guest can start a session, post a message, and read it back."""
        create_url = reverse("api:support:chat-session-create")
        created = self.client.post(create_url, {}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        session_id = created.data["id"]

        message_url = reverse("api:support:chat-message-create", args=[session_id])
        posted = self.client.post(message_url, {"body": "Hello"}, format="json")
        self.assertEqual(posted.status_code, status.HTTP_201_CREATED)
        self.assertEqual(posted.data["sender_type"], "customer")

        detail_url = reverse("api:support:chat-session-detail", args=[session_id])
        detail = self.client.get(detail_url)
        self.assertEqual(len(detail.data["messages"]), 1)

    def test_guest_cannot_read_another_guests_session(self):
        """A session opened in one guest session is invisible to another."""
        session = ChatSession.objects.create(guest_session_key="someone-elses-key")
        url = reverse("api:support:chat-session-detail", args=[session.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_authenticated_customer_owns_session(self):
        """A logged-in customer's session is isolated to their account."""
        _login(self.client)
        create_url = reverse("api:support:chat-session-create")
        created = self.client.post(create_url, {}, format="json")
        session = ChatSession.objects.get(pk=created.data["id"])
        self.assertEqual(session.user_id, self.user.pk)

    def test_staff_can_post_agent_message(self):
        """A support agent posts a message tagged as an agent."""
        session = ChatSession.objects.create(user=self.user)
        _login(self.client, email="support@example.com")
        url = reverse("api:support:staff-chat-message-create", args=[session.pk])
        response = self.client.post(url, {"body": "How can I help?"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["sender_type"], "agent")

    def test_customer_cannot_reach_staff_chat_queue(self):
        """A customer token is forbidden from the staff chat queue."""
        _login(self.client)
        url = reverse("api:support:staff-chat-list")
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_message_ended_session(self):
        """A message cannot be posted to an ended session."""
        _login(self.client)
        create_url = reverse("api:support:chat-session-create")
        created = self.client.post(create_url, {}, format="json")
        session_id = created.data["id"]
        end_url = reverse("api:support:chat-session-end", args=[session_id])
        self.client.post(end_url, {}, format="json")
        message_url = reverse("api:support:chat-message-create", args=[session_id])
        response = self.client.post(message_url, {"body": "hi"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_cannot_reach_staff_chat_queue(self):
        """An unauthenticated caller cannot read the staff chat queue."""
        url = reverse("api:support:staff-chat-list")
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_anonymous_cannot_reach_staff_ticket_queue(self):
        """An unauthenticated caller cannot read the staff ticket queue."""
        url = reverse("api:support:staff-ticket-list")
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_customer_message_sender_type_forced_to_customer(self):
        """A customer's chat message can never be tagged as an agent."""
        _login(self.client)
        create_url = reverse("api:support:chat-session-create")
        session_id = self.client.post(create_url, {}, format="json").data["id"]
        message_url = reverse("api:support:chat-message-create", args=[session_id])
        response = self.client.post(message_url, {"body": "hello"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["sender_type"], "customer")
        message = ChatMessage.objects.get(pk=response.data["id"])
        self.assertEqual(message.sender_type, "customer")

    def test_chat_assign_requires_support_role_target(self):
        """A chat session cannot be assigned to a non-support user."""
        session = ChatSession.objects.create(user=self.user)
        _login(self.client, email="support@example.com")
        url = reverse("api:support:staff-chat-assign", args=[session.pk])
        response = self.client.post(url, {"agent_id": self.user.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_assigns_chat_to_another_support_agent(self):
        """A manager can assign a session to a second support agent."""
        _make_staff(email="manager@example.com", role="manager")
        second = _make_staff(email="second@example.com", role="support")
        session = ChatSession.objects.create(user=self.user)
        _login(self.client, email="manager@example.com")
        url = reverse("api:support:staff-chat-assign", args=[session.pk])
        response = self.client.post(url, {"agent_id": second.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        session.refresh_from_db()
        self.assertEqual(session.assigned_agent_id, second.pk)


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
        """Create two customers, staff of two roles, and a ticket with a photo."""
        cache.clear()
        self.owner = _make_user()
        self.other = _make_user(email="other@example.com", username="other")
        self.support = _make_staff(email="support@example.com", role="support")
        self.analyst = _make_staff(email="analyst@example.com", role="analyst")
        self.ticket = Ticket.objects.create(
            user=self.owner, category="other", subject="Broken fan"
        )
        self.attachment = _png_upload("receipt.png")
        self.message = TicketMessage.objects.create(
            ticket=self.ticket,
            sender=self.owner,
            is_staff_reply=False,
            body="See photo",
            attachment=self.attachment,
        )

    def test_attachment_stored_under_random_name(self):
        """The stored filename carries no client-supplied name."""
        stored = self.message.attachment.name
        self.assertTrue(stored.startswith("support/attachments/"))
        self.assertNotIn("receipt.png", stored)

    def test_owner_can_download_attachment(self):
        """The ticket owner can fetch their own attachment."""
        _login(self.client)
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_customer_cannot_download_attachment(self):
        """Another customer's download resolves to 404, never the file."""
        _login(self.client, email="other@example.com")
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

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
        """An analyst role is neither owner nor support, so resolves to 404."""
        _login(self.client, email="analyst@example.com")
        url = reverse("api:support:ticket-attachment-download", args=[self.message.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_message_without_attachment_resolves_to_404(self):
        """A message carrying no file yields 404, never an empty download."""
        plain = TicketMessage.objects.create(
            ticket=self.ticket, sender=self.owner, body="no file"
        )
        _login(self.client)
        url = reverse("api:support:ticket-attachment-download", args=[plain.pk])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_serializer_exposes_download_url_not_media_path(self):
        """The message shape points at the download route, never the media URL."""
        _login(self.client)
        url = reverse("api:support:ticket-detail", args=[self.ticket.pk])
        response = self.client.get(url)
        message = next(m for m in response.data["messages"] if m["attachment"])
        self.assertIn("/support/attachments/", message["attachment"])
        self.assertNotIn("/media/", message["attachment"])

    def test_staff_serializer_exposes_download_url(self):
        """Staff and customer shapes both use the protected download route."""
        _login(self.client, email="support@example.com")
        url = reverse("api:support:staff-ticket-detail", args=[self.ticket.pk])
        response = self.client.get(url)
        messages_with_files = [m for m in response.data["messages"] if m["attachment"]]
        self.assertTrue(messages_with_files)
        self.assertIn("/support/attachments/", messages_with_files[0]["attachment"])


class TicketIdempotencyTests(APITestCase):
    """Idempotency-key enforcement on ticket creation."""

    def setUp(self):
        """Create a customer and log in."""
        cache.clear()
        self.user = _make_user()
        _login(self.client)

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
        self.assertEqual(Ticket.objects.filter(user=self.user).count(), 1)

    def test_distinct_idempotency_keys_create_distinct_tickets(self):
        """Different keys for the same payload are treated as separate tickets."""
        url = reverse("api:support:ticket-list-create")
        payload = {"subject": "Q", "category": "other", "message": "hi"}
        first = _post_ticket(self.client, url, payload, idempotency_key="key-a")
        second = _post_ticket(self.client, url, payload, idempotency_key="key-b")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(second.data["id"], first.data["id"])
        self.assertEqual(Ticket.objects.filter(user=self.user).count(), 2)
