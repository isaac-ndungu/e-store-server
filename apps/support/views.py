"""API views for the support app.

Endpoints split by caller type:

- Customer ticket endpoints require an authenticated customer. Ownership is
  enforced against ``request.user`` on every read and write — a ticket without
  an owning user, or one owned by someone else, resolves to 404, never 403, so
  nothing reveals whether a ticket id exists. The ``Ticket`` model carries no
  guest lookup token, so ticket self-service is deliberately login-only.
- Customer chat endpoints are open to guests as well: an authenticated caller
  owns a session by ``user``, a guest by their own Django session key, mirroring
  guest carts and guest orders.
- Staff endpoints are gated by the manager/support role and operate across every
  ticket and session.

All mutations go through the support service; no view writes a status, message
flag, or assignment field directly. Free-form text is sanitised in the service,
and attachments are validated by content and size here before storage.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, Http404
from rest_framework import permissions, serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.orders.selectors import get_order_for_user
from apps.returns.models import ReturnRequest
from apps.support.selectors import (
    get_chat_session_for_owner,
    get_chat_session_for_staff,
    get_ticket_for_staff,
    get_ticket_for_user,
    get_ticket_message_with_attachment,
    list_all_chat_sessions,
    list_all_tickets,
    list_tickets_for_user,
)
from apps.support.serializers import (
    ChatAssignSerializer,
    ChatMessageCreateSerializer,
    ChatMessageSerializer,
    ChatSessionSerializer,
    CustomerTicketDetailSerializer,
    CustomerTicketListSerializer,
    CustomerTicketMessageSerializer,
    StaffChatSessionListSerializer,
    StaffTicketDetailSerializer,
    StaffTicketListQuerySerializer,
    StaffTicketListSerializer,
    StaffTicketMessageSerializer,
    TicketAssignSerializer,
    TicketCreateSerializer,
    TicketMessageCreateSerializer,
    TicketStatusSerializer,
)
from apps.support.services import (
    add_chat_message,
    add_customer_message,
    add_staff_reply,
    assign_chat_agent,
    assign_ticket,
    create_ticket,
    end_chat_session,
    set_ticket_status,
    start_chat_session,
)
from apps.support.validators import validate_ticket_attachment


def _ensure_guest_session(request):
    """Return the request's Django session key, creating a session if needed.

    Args:
        request: the incoming HTTP request.

    Returns:
        str: the session key.
    """
    if request.session.session_key is None:
        request.session.create()
    return request.session.session_key


def _validated_attachment(request):
    """Return a validated attachment from the request, or None.

    The file type is checked by content and the size is capped before the file
    is handed to the service for storage.

    Args:
        request: the multipart HTTP request.

    Returns:
        UploadedFile | None: the validated attachment, or None when absent.

    Raises:
        serializers.ValidationError: when the file fails content/size checks.
    """
    uploaded = request.FILES.get("attachment")
    if uploaded is None:
        return None
    try:
        validate_ticket_attachment(uploaded)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"attachment": exc.messages}) from exc
    return uploaded


def _resolve_agent(agent_id):
    """Return a staff user for assignment or raise a 400.

    Args:
        agent_id (int): the candidate agent's user id.

    Returns:
        User: the resolved user (role is validated by the service).

    Raises:
        serializers.ValidationError: when no user matches the id.
    """
    agent = User.objects.filter(pk=agent_id).first()
    if agent is None:
        raise serializers.ValidationError({"agent_id": "No such user."})
    return agent


class TicketListCreateView(APIView):
    """List the caller's own tickets or open a new one."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]

    @property
    def throttle_scope(self):
        """Pick the write-rate scope for creation, the read scope for listing.

        Returns:
            str: the DRF throttle scope for the current request method.
        """
        return "support_write" if self.request.method == "POST" else "support_read"

    def get(self, request):
        """Return the authenticated caller's tickets, newest first.

        Args:
            request: the GET request.

        Returns:
            Response: the paginated ticket list.
        """
        tickets = list_tickets_for_user(request.user)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(tickets, request)
        serializer = CustomerTicketListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        """Open a ticket as the authenticated caller.

        A referenced ``order_id`` / ``return_request_id`` is re-validated
        against the caller's own orders and returns; a foreign or missing
        reference is rejected rather than linked, so a ticket can never be
        attached to a stranger's transaction.

        Args:
            request: the POST request carrying the ticket payload.

        Returns:
            Response: ``201 Created`` with the ticket detail, ``400`` for a
                validation failure, or ``409`` when the same idempotency key
                is already being processed.
        """
        from apps.core.idempotency import (
            acquire_processing_lock,
            read_cached_result,
            release_processing_lock,
            require_idempotency_key,
            store_result,
        )

        key = require_idempotency_key(request)
        user_pk = request.user.pk
        cached = read_cached_result(user_pk, key)
        if cached is not None:
            return Response(cached["data"], status=cached["status"])
        if not acquire_processing_lock(user_pk, key):
            return Response(
                {
                    "detail": "A request with this Idempotency-Key is already in progress."
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            input_serializer = TicketCreateSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            data = input_serializer.validated_data

            order = None
            if data.get("order_id") is not None:
                order = get_order_for_user(request.user, data["order_id"])
                if order is None:
                    raise serializers.ValidationError(
                        {"order_id": "No such order for this account."}
                    )

            return_request = None
            if data.get("return_request_id") is not None:
                return_request = ReturnRequest.objects.filter(
                    pk=data["return_request_id"], order__user=request.user
                ).first()
                if return_request is None:
                    raise serializers.ValidationError(
                        {"return_request_id": "No such return for this account."}
                    )

            ticket = _service_error_to_400(create_ticket)(
                user=request.user,
                subject=data["subject"],
                category=data["category"],
                message_body=data["message"],
                order=order,
                return_request=return_request,
            )
            serializer = CustomerTicketDetailSerializer(
                ticket, context={"request": request}
            )
            store_result(user_pk, key, status.HTTP_201_CREATED, serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        finally:
            release_processing_lock(user_pk, key)


class TicketDetailView(APIView):
    """Retrieve one of the caller's own tickets with its message thread."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_read"

    def get(self, request, ticket_id):
        """Return the caller's ticket detail.

        Args:
            request: the GET request.
            ticket_id (int): the ticket primary key.

        Returns:
            Response: the ticket detail, or 404 when the ticket is not the
                caller's.
        """
        ticket = get_ticket_for_user(request.user, ticket_id)
        if ticket is None:
            raise Http404
        serializer = CustomerTicketDetailSerializer(
            ticket, context={"request": request}
        )
        return Response(serializer.data)


class TicketMessageCreateView(APIView):
    """Append a message to one of the caller's own tickets."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_write"

    def post(self, request, ticket_id):
        """Post a customer message on the caller's ticket.

        Args:
            request: the multipart POST request carrying ``body`` and an
                optional ``attachment``.
            ticket_id (int): the ticket primary key.

        Returns:
            Response: ``201 Created`` with the message, ``400`` when the ticket
                is closed or the attachment is invalid, or ``404`` when the
                ticket is not the caller's.
        """
        ticket = get_ticket_for_user(request.user, ticket_id)
        if ticket is None:
            raise Http404
        input_serializer = TicketMessageCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        attachment = _validated_attachment(request)
        message = _service_error_to_400(add_customer_message)(
            ticket=ticket,
            user=request.user,
            body=input_serializer.validated_data["body"],
            attachment=attachment,
        )
        serializer = CustomerTicketMessageSerializer(
            message, context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TicketAttachmentDownloadView(APIView):
    """Stream a ticket attachment to the ticket owner or to support staff.

    Attachments hold customer PII (receipts, fault photos), so the file bytes
    are never exposed through a public media URL. Access is authorised per
    request: the ticket's own customer may fetch it, and manager/support staff
    may fetch any of them. A message that is missing, carries no attachment, or
    belongs to another customer resolves to 404 so nothing reveals which ids or
    files exist.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_read"

    def get(self, request, message_id):
        """Return the attachment file for an authorised caller.

        Args:
            request: the GET request.
            message_id (int): the ticket message primary key.

        Returns:
            FileResponse: the attachment as an attachment-disposition download.

        Raises:
            Http404: when the message, its attachment, or the caller's access
                to it does not resolve.
        """
        message = get_ticket_message_with_attachment(message_id)
        if message is None:
            raise Http404
        is_owner = (
            message.ticket.user_id is not None
            and message.ticket.user_id == request.user.pk
        )
        is_support = request.user.has_role("manager", "support")
        if not (is_owner or is_support):
            raise Http404
        return FileResponse(message.attachment.open("rb"), as_attachment=True)


class StaffTicketListView(APIView):
    """List every ticket for the support queue, with optional filters."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request):
        """Return tickets filtered by optional status/category/assignee.

        Args:
            request: the GET request with optional ``status``, ``category``,
                and ``assigned_to`` query params.

        Returns:
            Response: the paginated ticket list for the queue.
        """
        query = StaffTicketListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data
        tickets = list_all_tickets(
            status=filters.get("status"),
            category=filters.get("category"),
            assigned_to=filters.get("assigned_to"),
        )
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(tickets, request)
        serializer = StaffTicketListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


def _staff_ticket_or_404(ticket_id):
    """Return the staff-facing ticket or raise HTTP 404.

    Args:
        ticket_id (int): the ticket primary key.

    Returns:
        Ticket: the matched ticket.

    Raises:
        Http404: when no ticket matches the id.
    """
    ticket = get_ticket_for_staff(ticket_id)
    if ticket is None:
        raise Http404
    return ticket


class StaffTicketDetailView(APIView):
    """Retrieve any ticket with its message thread for support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request, ticket_id):
        """Return the ticket detail for the support console.

        Args:
            request: the GET request.
            ticket_id (int): the ticket primary key.

        Returns:
            Response: the ticket detail, or 404 when absent.
        """
        ticket = _staff_ticket_or_404(ticket_id)
        serializer = StaffTicketDetailSerializer(ticket, context={"request": request})
        return Response(serializer.data)


class StaffTicketReplyView(APIView):
    """Post a staff reply to a ticket."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, ticket_id):
        """Reply to the ticket as staff, marking it awaiting the customer.

        Args:
            request: the multipart POST request carrying ``body`` and an
                optional ``attachment``.
            ticket_id (int): the ticket primary key.

        Returns:
            Response: ``201 Created`` with the reply, ``400`` when the ticket
                is closed or the attachment is invalid, or ``404`` when absent.
        """
        ticket = _staff_ticket_or_404(ticket_id)
        input_serializer = TicketMessageCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        attachment = _validated_attachment(request)
        message = _service_error_to_400(add_staff_reply)(
            ticket=ticket,
            user=request.user,
            body=input_serializer.validated_data["body"],
            attachment=attachment,
        )
        serializer = StaffTicketMessageSerializer(message, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class StaffTicketAssignView(APIView):
    """Assign a ticket to a support agent."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, ticket_id):
        """Assign the ticket to the named agent.

        Args:
            request: the POST request carrying ``agent_id``.
            ticket_id (int): the ticket primary key.

        Returns:
            Response: ``200 OK`` with the ticket, ``400`` when the target is
                not support staff, or ``404`` when the ticket is absent.
        """
        ticket = _staff_ticket_or_404(ticket_id)
        input_serializer = TicketAssignSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        agent = _resolve_agent(input_serializer.validated_data["agent_id"])
        updated = _service_error_to_400(assign_ticket)(ticket=ticket, agent=agent)
        serializer = StaffTicketDetailSerializer(updated, context={"request": request})
        return Response(serializer.data)


class StaffTicketStatusView(APIView):
    """Change a ticket's status as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, ticket_id):
        """Move the ticket to the requested status.

        Args:
            request: the POST request carrying ``status``.
            ticket_id (int): the ticket primary key.

        Returns:
            Response: ``200 OK`` with the ticket, ``400`` for a disallowed
                transition, or ``404`` when absent.
        """
        ticket = _staff_ticket_or_404(ticket_id)
        input_serializer = TicketStatusSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(set_ticket_status)(
            ticket=ticket,
            to_status=input_serializer.validated_data["status"],
            user=request.user,
        )
        serializer = StaffTicketDetailSerializer(updated, context={"request": request})
        return Response(serializer.data)


class ChatSessionCreateView(APIView):
    """Open a live-chat session as a logged-in customer or a guest."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_write"

    def post(self, request):
        """Start a chat session owned by the caller.

        Args:
            request: the POST request.

        Returns:
            Response: ``201 Created`` with the new session.
        """
        if request.user.is_authenticated:
            session = _service_error_to_400(start_chat_session)(user=request.user)
        else:
            session_key = _ensure_guest_session(request)
            session = _service_error_to_400(start_chat_session)(
                guest_session_key=session_key
            )
        serializer = ChatSessionSerializer(session)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


def _resolve_chat_session(request, session_id):
    """Return the caller's own chat session or raise HTTP 404.

    An authenticated caller owns a session by ``user``; a guest owns it by their
    own session key. A foreign or missing id resolves to 404, never 403.

    Args:
        request: the incoming HTTP request.
        session_id (int): the chat session primary key.

    Returns:
        ChatSession: the resolved session.

    Raises:
        Http404: when the session is absent or not the caller's.
    """
    if request.user.is_authenticated:
        session = get_chat_session_for_owner(
            user=request.user, guest_session_key="", session_id=session_id
        )
    else:
        session_key = _ensure_guest_session(request)
        session = get_chat_session_for_owner(
            user=None, guest_session_key=session_key, session_id=session_id
        )
    if session is None:
        raise Http404
    return session


class ChatSessionDetailView(APIView):
    """Retrieve the caller's own chat session with its messages."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_read"

    def get(self, request, session_id):
        """Return the caller's chat session detail.

        Args:
            request: the GET request.
            session_id (int): the chat session primary key.

        Returns:
            Response: the session detail, or 404 when not the caller's.
        """
        session = _resolve_chat_session(request, session_id)
        serializer = ChatSessionSerializer(session)
        return Response(serializer.data)


class ChatMessageCreateView(APIView):
    """Post a customer message to the caller's own chat session."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_write"

    def post(self, request, session_id):
        """Append a customer message to the caller's session.

        The ``sender_type`` is fixed to ``customer`` server-side, so a caller
        on this endpoint can never post as an agent.

        Args:
            request: the POST request carrying ``body``.
            session_id (int): the chat session primary key.

        Returns:
            Response: ``201 Created`` with the message, ``400`` when the
                session has ended, or ``404`` when not the caller's.
        """
        session = _resolve_chat_session(request, session_id)
        input_serializer = ChatMessageCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        message = _service_error_to_400(add_chat_message)(
            session=session,
            sender_type="customer",
            body=input_serializer.validated_data["body"],
        )
        serializer = ChatMessageSerializer(message)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ChatSessionEndView(APIView):
    """End the caller's own chat session."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_write"

    def post(self, request, session_id):
        """Mark the caller's session ended.

        Args:
            request: the POST request.
            session_id (int): the chat session primary key.

        Returns:
            Response: ``200 OK`` with the ended session, or ``404`` when not
                the caller's.
        """
        session = _resolve_chat_session(request, session_id)
        ended = end_chat_session(session=session)
        serializer = ChatSessionSerializer(ended)
        return Response(serializer.data)


class StaffChatSessionListView(APIView):
    """List chat sessions for the support queue."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request):
        """Return chat sessions filtered by the optional ``active`` flag.

        Args:
            request: the GET request (``?active=true|false``).

        Returns:
            Response: the paginated session list.
        """
        active = _parse_active_param(request)
        sessions = list_all_chat_sessions(active=active)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(sessions, request)
        serializer = StaffChatSessionListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


def _staff_chat_or_404(session_id):
    """Return the staff-facing chat session or raise HTTP 404.

    Args:
        session_id (int): the chat session primary key.

    Returns:
        ChatSession: the matched session.

    Raises:
        Http404: when no session matches the id.
    """
    session = get_chat_session_for_staff(session_id)
    if session is None:
        raise Http404
    return session


class StaffChatSessionDetailView(APIView):
    """Retrieve any chat session with its messages for support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request, session_id):
        """Return the chat session detail for the support console.

        Args:
            request: the GET request.
            session_id (int): the chat session primary key.

        Returns:
            Response: the session detail, or 404 when absent.
        """
        session = _staff_chat_or_404(session_id)
        serializer = ChatSessionSerializer(session)
        return Response(serializer.data)


class StaffChatMessageCreateView(APIView):
    """Post an agent message to a chat session."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, session_id):
        """Append an agent message to the session.

        Args:
            request: the POST request carrying ``body``.
            session_id (int): the chat session primary key.

        Returns:
            Response: ``201 Created`` with the message, ``400`` when the
                session has ended, or ``404`` when absent.
        """
        session = _staff_chat_or_404(session_id)
        input_serializer = ChatMessageCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        message = _service_error_to_400(add_chat_message)(
            session=session,
            sender_type="agent",
            body=input_serializer.validated_data["body"],
        )
        serializer = ChatMessageSerializer(message)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class StaffChatAssignView(APIView):
    """Assign a chat session to a support agent."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, session_id):
        """Assign the session to the named agent.

        Args:
            request: the POST request carrying ``agent_id``.
            session_id (int): the chat session primary key.

        Returns:
            Response: ``200 OK`` with the session, ``400`` when the target is
                not support staff, or ``404`` when the session is absent.
        """
        session = _staff_chat_or_404(session_id)
        input_serializer = ChatAssignSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        agent = _resolve_agent(input_serializer.validated_data["agent_id"])
        updated = _service_error_to_400(assign_chat_agent)(session=session, agent=agent)
        serializer = StaffChatSessionListSerializer(updated)
        return Response(serializer.data)


def _parse_active_param(request):
    """Parse the optional ``active`` filter as a strict tri-state.

    Args:
        request: the HTTP request carrying the query string.

    Returns:
        bool | None: True/False to filter on that state, None for no filter.
    """
    raw = request.query_params.get("active")
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised in ("true", "1", "yes"):
        return True
    if normalised in ("false", "0", "no"):
        return False
    return None
