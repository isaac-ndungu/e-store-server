"""Read-only query helpers for the support app.

Selectors encapsulate query construction so views never build raw querysets
directly. Ticket reads span every ticket (internal staff tracking); a missing
id returns ``None`` so the view maps it to a 404. Message relations are
prefetched so a thread renders in a bounded number of queries rather than one
per message.
"""

from apps.support.models import Ticket, TicketMessage


def _ticket_base():
    """Return a ticket queryset with author and assignee joined.

    Returns:
        QuerySet: tickets with ``user`` and ``assigned_to`` selected.
    """
    return Ticket.objects.select_related("user", "assigned_to", "order")


def list_all_tickets(*, status=None, category=None, assigned_to=None):
    """Return tickets for the staff queue, optionally filtered.

    Args:
        status (str | None): restrict to a single status when given.
        category (str | None): restrict to a single category when given.
        assigned_to (int | None): restrict to tickets assigned to this user id
            when given.

    Returns:
        QuerySet: matching tickets, newest first.
    """
    queryset = _ticket_base()
    if status is not None:
        queryset = queryset.filter(status=status)
    if category is not None:
        queryset = queryset.filter(category=category)
    if assigned_to is not None:
        queryset = queryset.filter(assigned_to_id=assigned_to)
    return queryset


def get_ticket_for_staff(ticket_id):
    """Return any ticket by id with its messages, or None.

    Args:
        ticket_id (int): the ticket primary key.

    Returns:
        Ticket | None: the ticket, or None when no ticket matches.
    """
    return (
        _ticket_base().filter(pk=ticket_id).prefetch_related("messages__sender").first()
    )


def get_ticket_message_with_attachment(message_id):
    """Return a ticket message that carries an attachment, or None.

    The message is loaded with its ticket (and the ticket's owner) so the
    download view can check access without a second query. A message without a
    stored attachment resolves to ``None`` so a missing file is a 404, not an
    empty download.

    Args:
        message_id (int): the ticket message primary key.

    Returns:
        TicketMessage | None: the message when it exists and has an
            attachment, else None.
    """
    message = (
        TicketMessage.objects.select_related("ticket", "ticket__user")
        .filter(pk=message_id)
        .first()
    )
    if message is None or not message.attachment:
        return None
    return message
