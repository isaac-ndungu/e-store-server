"""API views for the inquiries app.

``InquiryCreateView`` is the single write endpoint an anonymous visitor
touches: public, throttled, no auth. The cart lines are read server-side
from the visitor's cookie-linked cart  -  the client never submits them  -
with a validated client snapshot accepted only when no server cart exists.
Staff queue views require staff access and stay paginated.
"""

import hmac

from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework import serializers as drf_serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cart.selectors import build_cart_snapshot, get_cart_from_cookie
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.core.http import client_ip
from apps.core.idempotency import (
    IDEMPOTENCY_HEADER,
    acquire_processing_lock,
    conflicting_key_response,
    payload_conflict,
    read_cached_result,
    release_processing_lock,
    request_fingerprint,
    store_result,
)
from apps.core.permissions import IsStaffUser
from apps.core.throttling import ClientIPScopedRateThrottle as ScopedRateThrottle
from apps.inquiries.email import resend_order_intake_email
from apps.inquiries.models import Inquiry
from apps.inquiries.selectors import list_inquiries
from apps.inquiries.serializers import (
    InquiryBulkSpamSerializer,
    InquiryCreateSerializer,
    InquiryDetailSerializer,
    InquiryMarkReceivedSerializer,
    InquiryRecoveryContactSerializer,
    InquiryStatusUpdateSerializer,
)
from apps.inquiries.services import (
    bulk_mark_inquiries_spam,
    find_inquiry_by_reference,
    mark_inquiry_message_received,
    record_inquiry,
    transition_inquiry,
    update_inquiry_recovery_contact,
)


def _is_excluded_inquiry_request(request, validated_data):
    """Return True when the request comes from staff or carries a test flag."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_staff:
        return True
    if validated_data.get("is_test"):
        return True
    header_flag = (request.headers.get("X-Test-Inquiry") or "").strip().lower()
    if header_flag in ("1", "true", "yes"):
        return True
    query_flag = (request.query_params.get("test") or "").strip().lower()
    if query_flag in ("1", "true", "yes"):
        return True
    return False


class InquiryCreateView(APIView):
    """Capture an anonymous WhatsApp/email hand-off (public).

    Fire-and-forget from the storefront: the visitor's WhatsApp/mail link
    opens regardless of this call's outcome, so the view does the minimum  -
    validate, store, acknowledge  -  and never blocks on external work.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_write"

    @extend_schema(
        operation_id="inquiry_create",
        request=InquiryCreateSerializer,
        responses={201: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def post(self, request):
        """Store the hand-off snapshot.

        The lines come from the visitor's server-side cart; a
        client-submitted snapshot is used only when no cart exists. When an
        ``Idempotency-Key`` is supplied, a repeated submission replays the
        first row instead of creating a duplicate.

        Args:
            request: the POST request with channel + optional snapshot.

        Returns:
            Response: ``201 Created`` with the stored inquiry.
        """
        cart = get_cart_from_cookie(request.COOKIES)
        idempotency_key = request.headers.get(IDEMPOTENCY_HEADER)
        scope = f"cart:{cart.pk}" if cart is not None else f"ip:{client_ip(request)}"
        fingerprint = request_fingerprint(request) if idempotency_key else None
        lock_acquired = False

        if idempotency_key:
            cached = read_cached_result(scope, idempotency_key)
            if cached is not None:
                if payload_conflict(cached, fingerprint):
                    return conflicting_key_response()
                return Response(cached["data"], status=cached["status"])
            lock_acquired = acquire_processing_lock(scope, idempotency_key)
            if not lock_acquired:
                return Response(
                    {"detail": "This request is already being processed."},
                    status=409,
                )

        try:
            input_serializer = InquiryCreateSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            data = input_serializer.validated_data
            if _is_excluded_inquiry_request(request, data):
                return Response(
                    {
                        "excluded": True,
                        "reference": data.get("reference_code") or "",
                        "channel": data["channel"],
                    },
                    status=200,
                )
            if cart is not None and cart.items.exists():
                cart_snapshot = build_cart_snapshot(cart)
            elif data.get("cart_snapshot"):
                cart_snapshot = data["cart_snapshot"]
            else:
                raise drf_serializers.ValidationError(
                    {"cart_snapshot": "No items in the cart to inquire about."}
                )
            inquiry = record_inquiry(
                channel=data["channel"],
                cart_snapshot=cart_snapshot,
                contact_hint=data.get("contact_hint", ""),
                customer_name=data.get("customer_name", ""),
                customer_phone=data.get("customer_phone", ""),
                customer_email=data.get("customer_email", ""),
                delivery_location=data.get("delivery_location", ""),
                order_notes=data.get("order_notes", ""),
                idempotency_key=idempotency_key or "",
                reference_code=data.get("reference_code", ""),
                page_url=data.get("page_url", ""),
            )
            inquiry.refresh_from_db()
            response_data = dict(InquiryDetailSerializer(inquiry).data)
            response_data["recovery_token"] = inquiry.recovery_token
            if idempotency_key:
                store_result(
                    scope,
                    idempotency_key,
                    201,
                    response_data,
                    payload_hash=fingerprint,
                )
            return Response(response_data, status=201)
        finally:
            if idempotency_key and lock_acquired:
                release_processing_lock(scope, idempotency_key)


class InquiryRecoveryContactView(APIView):
    """Attach optional name and phone from the post-tap recovery box (public)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_write"

    @extend_schema(
        operation_id="inquiry_recovery_contact",
        request=InquiryRecoveryContactSerializer,
        responses={200: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def patch(self, request, reference):
        """Update optional contact details on an inquiry using its recovery token.

        Args:
            request: the PATCH request with ``customer_name``/``customer_phone``
                and ``recovery_token`` (or ``X-Recovery-Token`` header).
            reference (str): the inquiry reference code (e.g. ``VF-4K8P``).

        Returns:
            Response: ``200 OK`` with the updated inquiry.
        """
        inquiry = find_inquiry_by_reference(reference)
        if inquiry is None:
            raise Http404("Inquiry not found.")

        input_serializer = InquiryRecoveryContactSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        supplied_token = (
            request.headers.get("X-Recovery-Token")
            or data.get("recovery_token")
            or ""
        )
        if (
            not inquiry.recovery_token
            or not supplied_token
            or not hmac.compare_digest(supplied_token, inquiry.recovery_token)
        ):
            return Response(
                {"detail": "Invalid or missing recovery token."},
                status=403,
            )

        updated = update_inquiry_recovery_contact(
            inquiry,
            customer_name=data.get("customer_name", ""),
            customer_phone=data.get("customer_phone", ""),
        )
        return Response(InquiryDetailSerializer(updated).data)


class InquiryListView(APIView):
    """List the staff follow-up queue, newest first (staff only)."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_read"

    @extend_schema(
        operation_id="inquiry_list",
        responses={200: InquiryDetailSerializer(many=True)},
        tags=["inquiries"],
    )
    def get(self, request):
        """Return paginated inquiries, optionally filtered.

        Args:
            request: the GET request with optional ``status``/``channel``
                and a ``search`` string matching a reference code
                (``INQ-000123``) or contact-hint substring.

        Returns:
            Response: the paginated queue.
        """
        no_message_after_1h = (
            request.query_params.get("no_message_after_1h", "").lower()
            in ("1", "true", "yes")
        )
        has_phone = (
            request.query_params.get("has_phone", "").lower()
            in ("1", "true", "yes")
        )
        queryset = list_inquiries(
            status=request.query_params.get("status") or None,
            channel=request.query_params.get("channel") or None,
            email_status=request.query_params.get("email_status") or None,
            search=request.query_params.get("search") or None,
            no_message_after_1h=no_message_after_1h,
            has_phone=has_phone,
            product=request.query_params.get("product") or None,
        )
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(queryset, request)
        serializer = InquiryDetailSerializer(page, many=True)
        return Response(
            {
                "count": paginator.page.paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": serializer.data,
            }
        )


class InquiryStatusUpdateView(APIView):
    """Move an inquiry through the staff queue (staff only)."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_read"

    def get_object(self, inquiry_id):
        """Return the inquiry or raise 404.

        Args:
            inquiry_id (int): the inquiry pk.

        Returns:
            Inquiry: the row.

        Raises:
            Http404: when missing.
        """
        from apps.inquiries.models import Inquiry

        try:
            obj = Inquiry.objects.select_related("converted_order").get(pk=inquiry_id)
        except Inquiry.DoesNotExist as exc:
            raise Http404 from exc
        self.check_object_permissions(self.request, obj)
        return obj

    @extend_schema(
        operation_id="inquiry_status_update",
        request=InquiryStatusUpdateSerializer,
        responses={200: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def post(self, request, inquiry_id):
        """Transition the inquiry status.

        Args:
            request: the POST request with ``to_status``.
            inquiry_id (int): the inquiry pk.

        Returns:
            Response: ``200 OK`` with the updated inquiry.
        """
        inquiry = self.get_object(inquiry_id)
        input_serializer = InquiryStatusUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(transition_inquiry)(
            inquiry, input_serializer.validated_data["to_status"]
        )
        return Response(InquiryDetailSerializer(updated).data)


class InquiryResendEmailView(APIView):
    """Resend an email inquiry's order-intake email (staff only).

    The send is synchronous so the staff member sees the provider outcome
    immediately; the audit row and inquiry delivery fields are updated either
    way. WhatsApp inquiries have no mailbox copy to resend.
    """

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_resend"

    def get_object(self, inquiry_id):
        """Return the inquiry or raise 404.

        Args:
            inquiry_id (int): the inquiry pk.

        Returns:
            Inquiry: the row.

        Raises:
            Http404: when missing.
        """
        from apps.inquiries.models import Inquiry

        try:
            return Inquiry.objects.select_related("converted_order").get(pk=inquiry_id)
        except Inquiry.DoesNotExist as exc:
            raise Http404 from exc

    @extend_schema(
        operation_id="inquiry_resend_email",
        request=None,
        responses={200: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def post(self, request, inquiry_id):
        """Resend the order-intake email and return the updated inquiry.

        Args:
            request: the POST request.
            inquiry_id (int): the inquiry pk.

        Returns:
            Response: ``200 OK`` with the inquiry's latest delivery state.
        """
        inquiry = self.get_object(inquiry_id)
        if inquiry.channel != "email":
            return Response(
                {"detail": "Only email inquiries can be resent."},
                status=400,
            )
        resend_order_intake_email(inquiry, sent_by=request.user)
        inquiry.refresh_from_db()
        return Response(InquiryDetailSerializer(inquiry).data)


class InquiryMarkReceivedView(APIView):
    """Mark an inquiry as message_received by reference code (staff only)."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_read"

    @extend_schema(
        operation_id="inquiry_mark_received",
        request=InquiryMarkReceivedSerializer,
        responses={200: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def post(self, request):
        """Find the inquiry by reference code and mark its message received.

        Args:
            request: the POST request with ``reference``.

        Returns:
            Response: ``200 OK`` with the updated inquiry.
        """
        input_serializer = InquiryMarkReceivedSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        try:
            updated = _service_error_to_400(mark_inquiry_message_received)(
                input_serializer.validated_data["reference"]
            )
        except Inquiry.DoesNotExist as exc:
            raise Http404("No inquiry matches that reference code.") from exc
        return Response(InquiryDetailSerializer(updated).data)


class InquiryBulkSpamView(APIView):
    """Bulk-mark selected test or spam inquiries as spam (staff only)."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_read"

    @extend_schema(
        operation_id="inquiry_bulk_spam",
        request=InquiryBulkSpamSerializer,
        responses={200: dict},
        tags=["inquiries"],
    )
    def post(self, request):
        """Mark all non-converted selected inquiries as spam.

        Args:
            request: the POST request with ``inquiry_ids``.

        Returns:
            Response: ``200 OK`` with ``updated_count`` and ``skipped_ids``.
        """
        input_serializer = InquiryBulkSpamSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        result = bulk_mark_inquiries_spam(
            input_serializer.validated_data["inquiry_ids"]
        )
        return Response(result)
