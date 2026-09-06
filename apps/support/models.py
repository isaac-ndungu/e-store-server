"""Data models for the support app.

Two customer-support channels live here:

- Ticketing (``Ticket`` / ``TicketMessage``) — an asynchronous thread between a
  customer and staff, optionally tied to an order and to a return request. A
  ticket carries a status the customer's replies and staff actions move it
  through; every message is retained so the conversation is fully auditable.
- Live chat (``ChatSession`` / ``ChatMessage``) — a synchronous conversation
  that a logged-in customer or an anonymous guest can open. A guest session is
  addressed by the guest's own Django session key rather than a login, mirroring
  how guest carts and guest orders identify their owner.

Free-form message text is sanitised at the service boundary before storage, so
markup a customer or agent submits can never execute when the thread is later
rendered in the storefront or the staff console.
"""

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from apps.support.constants import (
    CHAT_MESSAGE_MAX_LENGTH,
    TICKET_SUBJECT_MAX_LENGTH,
)


def ticket_attachment_path(instance, filename):
    """Return an unguessable storage path for a ticket attachment.

    The client-supplied name is discarded in favour of a random UUID so an
    attachment cannot be located by guessing another customer's filename and
    a second upload of the same name cannot overwrite an earlier file. The
    original extension is preserved only to keep the stored file's type hint
    intact; access is still gated by the download view, never by the path.

    Args:
        instance (TicketMessage): the message the file is attached to.
        filename (str): the original client-supplied filename.

    Returns:
        str: the storage-relative path for the file.
    """
    suffix = Path(filename).suffix.lower()
    return f"support/attachments/{uuid.uuid4().hex}{suffix}"


class Ticket(models.Model):
    """A support ticket: one customer-to-staff conversation about an issue.

    ``user`` is nullable and ``SET_NULL`` so a ticket survives the deletion of
    the account that opened it (staff still need the history), and so staff can
    raise a ticket on a customer's behalf. Customer self-service access is
    nonetheless authenticated and ownership-checked — the model carries no guest
    lookup token, so a ticket without a ``user`` is reachable only by staff.

    ``order`` and the optional reverse link from ``returns.ReturnRequest.ticket``
    tie a ticket to the transaction it concerns without duplicating that data:
    order-time facts are read from the order/return, not copied onto the ticket.
    ``status`` is the single field describing where the ticket sits and is only
    moved through the support service, never written directly in a view.
    """

    STATUS_CHOICES = (
        ("open", "Open"),
        ("pending_customer", "Pending Customer"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
    )
    CATEGORY_CHOICES = (
        ("return", "Return/Refund"),
        ("complaint", "Complaint"),
        ("product_question", "Product Question"),
        ("order_issue", "Order Issue"),
        ("other", "Other"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets",
    )
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets",
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    subject = models.CharField(max_length=TICKET_SUBJECT_MAX_LENGTH)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="open", db_index=True
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"], name="ticket_user_created_idx"),
            models.Index(
                fields=["status", "category"], name="ticket_status_category_idx"
            ),
            models.Index(
                fields=["assigned_to", "status"], name="ticket_assignee_status_idx"
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the ticket."""
        return f"Ticket #{self.pk}: {self.subject} ({self.status})"


class TicketMessage(models.Model):
    """A single message in a ticket thread, from the customer or a staff member.

    ``is_staff_reply`` is set by the service from the caller's role, never from
    the request body, so a customer cannot mark their own message as an official
    staff reply. ``sender`` is ``SET_NULL`` so a message stays in the thread even
    if its author's account is later removed. ``attachment`` is validated by
    content and size at the view boundary before it reaches storage.
    """

    ticket = models.ForeignKey(
        Ticket, related_name="messages", on_delete=models.CASCADE
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_messages",
    )
    is_staff_reply = models.BooleanField(default=False)
    body = models.TextField()
    attachment = models.FileField(upload_to=ticket_attachment_path, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(
                fields=["ticket", "created_at"], name="ticketmsg_ticket_created_idx"
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the message."""
        author = "staff" if self.is_staff_reply else "customer"
        return f"Message on ticket {self.ticket_id} ({author})"


class ChatSession(models.Model):
    """A live-chat conversation opened by a logged-in customer or a guest.

    Exactly one owner identifier is populated: ``user`` for an authenticated
    customer, or ``guest_session_key`` for an anonymous visitor. The guest key
    is that visitor's own Django session key, so a guest can reach their own
    session without a login while another visitor cannot address it. ``user``
    and ``assigned_agent`` are both FKs to the user model and therefore carry
    distinct related names.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_sessions",
    )
    guest_session_key = models.CharField(max_length=100, blank=True, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="assigned_chat_sessions",
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["user", "started_at"], name="chat_user_started_idx"),
            models.Index(
                fields=["assigned_agent", "ended_at"], name="chat_agent_ended_idx"
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the chat session."""
        owner = f"user {self.user_id}" if self.user_id else "guest"
        return f"Chat session #{self.pk} ({owner})"


class ChatMessage(models.Model):
    """A single message in a live-chat session.

    ``sender_type`` records who authored the line — the customer, a staff agent,
    or an automated bot — and is set by the service from the authenticated
    context, never from the request body, so a customer cannot post a message
    that presents as an agent.
    """

    SENDER_CHOICES = (
        ("customer", "Customer"),
        ("agent", "Agent"),
        ("bot", "Bot"),
    )

    session = models.ForeignKey(
        ChatSession, related_name="messages", on_delete=models.CASCADE
    )
    sender_type = models.CharField(max_length=10, choices=SENDER_CHOICES)
    body = models.TextField(max_length=CHAT_MESSAGE_MAX_LENGTH)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sent_at", "pk"]
        indexes = [
            models.Index(
                fields=["session", "sent_at"], name="chatmsg_session_sent_idx"
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the chat message."""
        return f"Chat message on session {self.session_id} ({self.sender_type})"
