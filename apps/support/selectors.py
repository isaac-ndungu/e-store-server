"""Read-only query helpers for the support app.

Selectors encapsulate query construction so views never build raw querysets
directly. Customer reads are always scoped to the caller (their own tickets,
their own chat session); a missing or foreign id returns ``None`` so the view
maps both to a 404 without revealing whether the id exists. Staff reads span
every ticket and session. Message relations are prefetched so a thread renders
in a bounded number of queries rather than one per message.
"""

from apps.support.models import ChatSession, Ticket, TicketMessage


def _ticket_base():
    """Return a ticket queryset with author and assignee joined.

    Returns:
        QuerySet: tickets with ``user`` and ``assigned_to`` selected.
    """
    return Ticket.objects.select_related("user", "assigned_to", "order")


def list_tickets_for_user(user):
    """Return the caller's own tickets, newest first.

    Args:
        user (User): the authenticated caller.

    Returns:
        QuerySet: the user's tickets.
    """
    return _ticket_base().filter(user=user)


def get_ticket_for_user(user, ticket_id):
    """Return one of the caller's own tickets with its messages, or None.

    A ticket owned by another user, or a missing id, both resolve to ``None``
    so the view answers 404 without leaking which ids exist.

    Args:
        user (User): the authenticated caller.
        ticket_id (int): the ticket primary key.

    Returns:
        Ticket | None: the owned ticket, or None.
    """
    return (
        _ticket_base()
        .filter(pk=ticket_id, user=user)
        .prefetch_related("messages__sender")
        .first()
    )


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


def get_chat_session_for_owner(*, user, guest_session_key, session_id):
    """Return a chat session owned by the caller, or None.

    Ownership is by ``user`` for an authenticated caller and by the guest's own
    session key otherwise; the two never cross. A foreign or missing id
    resolves to ``None``.

    Args:
        user (User | None): the authenticated caller, or None for a guest.
        guest_session_key (str): the guest's Django session key (ignored when
            ``user`` is authenticated).
        session_id (int): the chat session primary key.

    Returns:
        ChatSession | None: the owned session, or None.
    """
    queryset = ChatSession.objects.filter(pk=session_id)
    if user is not None and user.is_authenticated:
        queryset = queryset.filter(user=user)
    elif guest_session_key:
        queryset = queryset.filter(
            user__isnull=True, guest_session_key=guest_session_key
        )
    else:
        return None
    return queryset.prefetch_related("messages").first()


def list_all_chat_sessions(*, active=None):
    """Return chat sessions for the staff queue, optionally filtered by state.

    Args:
        active (bool | None): when True, only sessions still open (no
            ``ended_at``); when False, only ended sessions; None for all.

    Returns:
        QuerySet: matching chat sessions, newest first.
    """
    queryset = ChatSession.objects.select_related("user", "assigned_agent")
    if active is True:
        queryset = queryset.filter(ended_at__isnull=True)
    elif active is False:
        queryset = queryset.filter(ended_at__isnull=False)
    return queryset


def get_chat_session_for_staff(session_id):
    """Return any chat session by id with its messages, or None.

    Args:
        session_id (int): the chat session primary key.

    Returns:
        ChatSession | None: the session, or None when no session matches.
    """
    return (
        ChatSession.objects.select_related("user", "assigned_agent")
        .filter(pk=session_id)
        .prefetch_related("messages")
        .first()
    )
