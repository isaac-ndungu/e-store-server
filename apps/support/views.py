"""API views for the support app.

Ticket endpoints are staff-only (manager/support): tickets are internal
issue tracking filed by staff after a customer complaint, so any staff
member reads and writes every ticket.

All mutations go through the support service; no view writes a status, message
flag, or assignment field directly. Free-form text is sanitised in the service,
and attachments are validated by content and size here before storage.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, Http404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.core.idempotency import (
    acquire_processing_lock,
    conflicting_key_response,
    payload_conflict,
    read_cached_result,
    release_processing_lock,
    request_fingerprint,
    require_idempotency_key,
    store_result,
)
from apps.orders.selectors import get_order_for_staff
from apps.returns.models import ReturnRequest
from apps.support.selectors import (
    get_ticket_for_staff,
    get_ticket_message_with_attachment,
    list_all_tickets,
)
from apps.support.serializers import (
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
    add_staff_reply,
    assign_ticket,
    create_ticket,
    set_ticket_status,
)
from apps.support.validators import validate_ticket_attachment


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
    """List every ticket or file a new one (staff only)."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]

    @property
    def throttle_scope(self):
        """Pick the write-rate scope for creation, the read scope for listing.

        Returns:
            str: the DRF throttle scope for the current request method.
        """
        return "support_write" if self.request.method == "POST" else "support_read"

    @extend_schema(
        operation_id="tickets_list",
        responses=StaffTicketListSerializer(many=True),
    )
    def get(self, request):
        """Return all tickets, newest first.

        Args:
            request: the GET request.

        Returns:
            Response: the paginated ticket list.
        """
        tickets = list_all_tickets()
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(tickets, request)
        serializer = StaffTicketListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        operation_id="tickets_create",
        request=TicketCreateSerializer,
        responses=StaffTicketDetailSerializer,
    )
    def post(self, request):
        """File a ticket as the staff caller.

        A referenced ``order_id`` / ``return_request_id`` must exist; tickets
        are internal records, so no ownership check applies.

        Args:
            request: the POST request carrying the ticket payload.

        Returns:
            Response: ``201 Created`` with the ticket detail, ``400`` for a
                validation failure, or ``409`` when the same idempotency key
                is already being processed.
        """
        key = require_idempotency_key(request)
        user_pk = request.user.pk
        fingerprint = request_fingerprint(request)
        cached = read_cached_result(user_pk, key)
        if cached is not None:
            if payload_conflict(cached, fingerprint):
                return conflicting_key_response()
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
                order = get_order_for_staff(data["order_id"])
                if order is None:
                    raise serializers.ValidationError({"order_id": "No such order."})

            return_request = None
            if data.get("return_request_id") is not None:
                return_request = ReturnRequest.objects.filter(
                    pk=data["return_request_id"]
                ).first()
                if return_request is None:
                    raise serializers.ValidationError(
                        {"return_request_id": "No such return request."}
                    )

            ticket = _service_error_to_400(create_ticket)(
                user=request.user,
                subject=data["subject"],
                category=data["category"],
                message_body=data["message"],
                order=order,
                return_request=return_request,
            )
            serializer = StaffTicketDetailSerializer(
                ticket, context={"request": request}
            )
            store_result(
                user_pk, key, status.HTTP_201_CREATED, serializer.data, fingerprint
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        finally:
            release_processing_lock(user_pk, key)


class TicketAttachmentDownloadView(APIView):
    """Stream a ticket attachment to support staff.

    Attachments hold customer PII (receipts, fault photos), so the file bytes
    are never exposed through a public media URL. Only manager/support staff
    may fetch them. A message that is missing or carries no attachment
    resolves to 404.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_read"

    @extend_schema(
        operation_id="ticket_attachment_download",
        responses={200: bytes, 404: None},
    )
    def get(self, request, message_id):
        """Return the attachment file for a staff caller.

        Args:
            request: the GET request.
            message_id (int): the ticket message primary key.

        Returns:
            FileResponse: the attachment as an attachment-disposition download.

        Raises:
            Http404: when the message or its attachment does not resolve.
        """
        message = get_ticket_message_with_attachment(message_id)
        if message is None:
            raise Http404
        return FileResponse(message.attachment.open("rb"), as_attachment=True)


class StaffTicketListView(APIView):
    """List every ticket for the support queue, with optional filters."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="staff_tickets_list",
        responses=StaffTicketListSerializer(many=True),
    )
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

    @extend_schema(
        operation_id="staff_ticket_detail",
        responses=StaffTicketDetailSerializer,
    )
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

    @extend_schema(
        operation_id="staff_ticket_reply",
        request=TicketMessageCreateSerializer,
        responses=StaffTicketMessageSerializer,
    )
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

    @extend_schema(
        operation_id="staff_ticket_assign",
        request=TicketAssignSerializer,
        responses=StaffTicketDetailSerializer,
    )
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

    @extend_schema(
        operation_id="staff_ticket_status",
        request=TicketStatusSerializer,
        responses=StaffTicketDetailSerializer,
    )
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
