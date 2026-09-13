"""Business logic for the support app.

The service layer is the only place ``Ticket`` and ``TicketMessage`` rows are
created or transitioned.

Invariants upheld here:

- Free-form message text (ticket subjects and bodies) is
  sanitised with ``bleach`` at this boundary, so markup and inline event
  handlers are stripped before the text is stored and later rendered.
- ``TicketMessage.is_staff_reply`` is derived
  from the authenticated caller, never from the request body, so a customer
  cannot post a message that presents as staff.
- A ticket's ``status`` moves only through ``set_ticket_status`` (staff) or the
  implicit reopen a customer reply triggers, and only along the allowed graph.
- A return-linked ticket writes the link onto the ``ReturnRequest`` in the same
  transaction as the ticket is created, so a refund dispute and its
  conversation are never left half-connected.
"""

import logging

import bleach
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.support.models import Ticket, TicketMessage

logger = logging.getLogger(__name__)

# A ticket status may only move along this graph. A closed ticket is terminal;
# reopening it means opening a new ticket, so the history of a closed dispute
# can never be silently reactivated.
_TICKET_STATUS_TRANSITIONS = {
    "open": {"pending_customer", "resolved", "closed"},
    "pending_customer": {"open", "resolved", "closed"},
    "resolved": {"open", "closed"},
    "closed": set(),
}

# Roles allowed to act on a ticket as staff (reply, assign, change status) and
# to be assigned tickets.
_SUPPORT_ROLES = ("manager", "support")


def _sanitize(value):
    """Strip all markup from free-form support text before it is stored.

    ``bleach`` removes every tag and its attributes while keeping the textual
    content, so a customer's or agent's prose survives while embedded scripts,
    iframes, and inline event handlers are dropped.

    Args:
        value (str): the raw text submitted by a customer or staff member.

    Returns:
        str: the sanitised text.
    """
    return bleach.clean(value, tags=set(), strip=True)


def create_ticket(
    *, user, subject, category, message_body, order=None, return_request=None
):
    """File a support ticket with the customer's complaint as first message.

    Tickets are internal staff tracking: ``user`` is the staff member filing,
    and the subject/message record the customer's complaint. The subject and
    message body are sanitised before storage. When a ``return_request`` is
    supplied, its ``ticket`` link is set in the same transaction and the
    ticket inherits the return's order if none was given, so a return dispute
    and its conversation stay connected.

    Args:
        user (User): the staff member filing the ticket.
        subject (str): the ticket subject line.
        category (str): a value from ``Ticket.CATEGORY_CHOICES``.
        message_body (str): the customer's complaint text.
        order (Order | None): the order the ticket concerns, if any.
        return_request (ReturnRequest | None): the return the ticket concerns,
            if any; its ``ticket`` link is set to the new ticket.

    Returns:
        Ticket: the created ticket, in ``open`` status.
    """
    with transaction.atomic():
        if order is None and return_request is not None:
            order = return_request.order
        ticket = Ticket.objects.create(
            user=user,
            order=order,
            category=category,
            subject=_sanitize(subject),
            status="open",
        )
        TicketMessage.objects.create(
            ticket=ticket,
            sender=user,
            is_staff_reply=False,
            body=_sanitize(message_body),
        )
        if return_request is not None:
            return_request.ticket = ticket
            return_request.save(update_fields=["ticket"])
    logger.info("Support ticket %s opened by user %s", ticket.pk, user.pk)
    return ticket


def add_staff_reply(*, ticket, user, body, attachment=None):
    """Append a staff reply to a ticket and mark it awaiting the customer.

    A reply moves an ``open`` ticket to ``pending_customer``; a ticket already
    resolved or closed is not reopened by a staff note. An unassigned ticket is
    claimed by the replying agent so the queue reflects who is handling it.

    Args:
        ticket (Ticket): the ticket to append to.
        user (User): the replying staff member.
        body (str): the reply text.
        attachment (UploadedFile | None): an already-validated attachment, or
            None.

    Returns:
        TicketMessage: the created message.

    Raises:
        ValidationError: when the ticket is closed.
    """
    if ticket.status == "closed":
        raise ValidationError("This ticket is closed and cannot receive new messages.")

    with transaction.atomic():
        message = TicketMessage.objects.create(
            ticket=ticket,
            sender=user,
            is_staff_reply=True,
            body=_sanitize(body),
            attachment=attachment or "",
        )
        update_fields = []
        if ticket.assigned_to_id is None:
            ticket.assigned_to = user
            update_fields.append("assigned_to")
        if ticket.status == "open":
            ticket.status = "pending_customer"
            update_fields.append("status")
        if update_fields:
            update_fields.append("updated_at")
            ticket.save(update_fields=update_fields)
    return message


def set_ticket_status(*, ticket, to_status, user):
    """Move a ticket to a new status as staff, validating the transition.

    Args:
        ticket (Ticket): the ticket to transition.
        to_status (str): the target status.
        user (User): the staff member performing the change.

    Returns:
        Ticket: the updated ticket.

    Raises:
        ValidationError: when the transition is not allowed from the current
            status.
    """
    allowed = _TICKET_STATUS_TRANSITIONS.get(ticket.status, set())
    if to_status not in allowed:
        raise ValidationError(
            f"Cannot move a ticket from '{ticket.status}' to '{to_status}'."
        )
    ticket.status = to_status
    ticket.save(update_fields=["status", "updated_at"])
    logger.info("Ticket %s moved to %s by staff user %s", ticket.pk, to_status, user.pk)
    return ticket


def assign_ticket(*, ticket, agent):
    """Assign a ticket to a support agent.

    Args:
        ticket (Ticket): the ticket to assign.
        agent (User): the staff member to assign it to.

    Returns:
        Ticket: the updated ticket.

    Raises:
        ValidationError: when the target user does not hold a support role.
    """
    if not agent.has_role(*_SUPPORT_ROLES):
        raise ValidationError("Tickets can only be assigned to support staff.")
    ticket.assigned_to = agent
    ticket.save(update_fields=["assigned_to", "updated_at"])
    return ticket
