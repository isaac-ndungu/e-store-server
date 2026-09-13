"""API views for the returns app.

All return endpoints are staff-only (manager/support): return requests are
filed by staff after a customer complaint, and the refund-recording and
pre-shipment cancellation actions require an ``Idempotency-Key`` so a
retried request cannot record the same refund twice.

Refunds are arranged by staff outside the system — these endpoints record
what happened, they never move money.

All mutations go through the returns service; no view writes a status or
money field directly.
"""

from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.orders.selectors import get_order_for_staff
from apps.orders.serializers import OrderDetailSerializer
from apps.returns.selectors import (
    get_return_request_for_order,
    get_return_request_for_staff,
    list_all_return_requests,
    list_return_requests_for_order,
)
from apps.returns.serializers import (
    PreShipmentCancelSerializer,
    ReturnApproveSerializer,
    ReturnRefundSerializer,
    ReturnRejectSerializer,
    ReturnRequestCreateSerializer,
    ReturnRequestDetailSerializer,
    ReturnRequestListSerializer,
)
from apps.returns.services import (
    approve_return_request,
    cancel_confirmed_order,
    close_return_request,
    create_return_request,
    record_item_received,
    refund_return_request,
    reject_return_request,
)


def _staff_return_or_404(return_request_id):
    """Return the staff-facing return request or raise HTTP 404.

    Args:
        return_request_id (int): the return request primary key.

    Returns:
        ReturnRequest: the matched request.

    Raises:
        Http404: when no request matches the id.
    """
    return_request = get_return_request_for_staff(return_request_id)
    if return_request is None:
        raise Http404
    return return_request


def _staff_order_or_404(order_id):
    """Return any order by id for a staff member, or raise HTTP 404.

    Args:
        order_id (int): the order primary key.

    Returns:
        Order: the matched order.

    Raises:
        Http404: when no order matches the id.
    """
    order = get_order_for_staff(order_id)
    if order is None:
        raise Http404
    return order


class OrderReturnRequestListCreateView(APIView):
    """List or file return requests for an order (staff only)."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="order_return_request_list",
        responses={200: ReturnRequestListSerializer(many=True)},
    )
    def get(self, request, order_id, *_args, **_kwargs):
        """Return the order's return requests, newest first.

        Args:
            request: the GET request.
            order_id (int): the order primary key.

        Returns:
            Response: the paginated return-request list for the order.
        """
        order = _staff_order_or_404(order_id)
        requests = list_return_requests_for_order(order)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(requests, request)
        serializer = ReturnRequestListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        operation_id="order_return_request_create",
        request=ReturnRequestCreateSerializer,
        responses={201: ReturnRequestDetailSerializer},
    )
    def post(self, request, order_id, *_args, **_kwargs):
        """File a return request against a delivered order.

        Args:
            request: the POST request carrying the return payload.
            order_id (int): the order primary key.

        Returns:
            Response: ``201 Created`` with the created request detail, or
                ``400`` for a validation/business-rule failure.
        """
        order = _staff_order_or_404(order_id)
        input_serializer = ReturnRequestCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        return_request = _service_error_to_400(create_return_request)(
            order=order,
            order_item_id=data.get("order_item_id"),
            reason=data["reason"],
            requested_resolution=data["requested_resolution"],
            user=request.user,
        )
        serializer = ReturnRequestDetailSerializer(return_request)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class OrderReturnRequestDetailView(APIView):
    """Retrieve a single return request on an order (staff only)."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="order_return_request_detail",
        responses={200: ReturnRequestDetailSerializer},
    )
    def get(self, request, order_id, return_request_id):
        """Return the matching return request with full detail.

        Args:
            request: the GET request.
            order_id (int): the order primary key.
            return_request_id (int): the return request primary key.

        Returns:
            Response: the full return request detail.

        Raises:
            Http404: when the order or request does not exist.
        """
        order = _staff_order_or_404(order_id)
        return_request = get_return_request_for_order(order, return_request_id)
        if return_request is None:
            raise Http404
        serializer = ReturnRequestDetailSerializer(return_request)
        return Response(serializer.data)


class ReturnRequestStaffListView(APIView):
    """List every return request for managers and support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_return_request_list",
        responses={200: ReturnRequestListSerializer(many=True)},
    )
    def get(self, request):
        """Return all return requests, paginated newest-first.

        Args:
            request: the GET request.

        Returns:
            Response: the paginated return request list.
        """
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(list_all_return_requests(), request)
        serializer = ReturnRequestListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ReturnRequestStaffDetailView(APIView):
    """Retrieve one return request for managers and support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_return_request_detail",
        responses={200: ReturnRequestDetailSerializer},
    )
    def get(self, request, return_request_id):
        """Return the matching return request with full detail.

        Args:
            request: the GET request.
            return_request_id (int): the return request primary key.

        Returns:
            Response: the full return request detail.

        Raises:
            Http404: when no request matches the id.
        """
        return_request = _staff_return_or_404(return_request_id)
        serializer = ReturnRequestDetailSerializer(return_request)
        return Response(serializer.data)


class ReturnApproveView(APIView):
    """Approve a return request and fix its refund economics as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_return_approve",
        request=ReturnApproveSerializer,
        responses={200: ReturnRequestDetailSerializer},
    )
    def post(self, request, return_request_id):
        """Approve the request.

        Args:
            request: the POST request carrying the approval payload.
            return_request_id (int): the return request primary key.

        Returns:
            Response: ``200 OK`` with the approved request, ``400`` for a
                business-rule failure, or ``404`` when absent.
        """
        return_request = _staff_return_or_404(return_request_id)
        input_serializer = ReturnApproveSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        approved = _service_error_to_400(approve_return_request)(
            return_request=return_request,
            refund_method=data.get("refund_method", ""),
            refund_amount=data.get("refund_amount"),
            restocking_fee_amount=data.get("restocking_fee"),
            user=request.user,
        )
        serializer = ReturnRequestDetailSerializer(approved)
        return Response(serializer.data)


class ReturnRejectView(APIView):
    """Reject a return request as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_return_reject",
        request=ReturnRejectSerializer,
        responses={200: ReturnRequestDetailSerializer},
    )
    def post(self, request, return_request_id):
        """Reject the request.

        ``note`` records the staff reason in the audit trail. A request that
        has already been restocked cannot be rejected — it must be closed,
        refunded, or replaced.

        Args:
            request: the POST request carrying an optional ``note``.
            return_request_id (int): the return request primary key.

        Returns:
            Response: ``200 OK`` with the rejected request, or ``404`` when
                absent.
        """
        return_request = _staff_return_or_404(return_request_id)
        input_serializer = ReturnRejectSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        note = input_serializer.validated_data.get("note", "")
        updated = _service_error_to_400(reject_return_request)(
            return_request=return_request,
            note=note,
            user=request.user,
        )
        serializer = ReturnRequestDetailSerializer(updated)
        return Response(serializer.data)


class ReturnCloseView(APIView):
    """Close a return request without completing a refund or replacement."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_return_close",
        request=ReturnRejectSerializer,
        responses={200: ReturnRequestDetailSerializer},
    )
    def post(self, request, return_request_id):
        """Close the request.

        Args:
            request: the POST request carrying an optional ``note``.
            return_request_id (int): the return request primary key.

        Returns:
            Response: ``200 OK`` with the closed request, or ``404`` when
                absent.
        """
        return_request = _staff_return_or_404(return_request_id)
        input_serializer = ReturnRejectSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        note = input_serializer.validated_data.get("note", "")
        updated = _service_error_to_400(close_return_request)(
            return_request=return_request,
            note=note,
            user=request.user,
        )
        serializer = ReturnRequestDetailSerializer(updated)
        return Response(serializer.data)


class ReturnReceiveItemView(APIView):
    """Record physical receipt of returned goods as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    schema = None

    @extend_schema(
        operation_id="admin_return_receive_item",
        responses={200: ReturnRequestDetailSerializer},
    )
    def post(self, request, return_request_id):
        """Receive the returned goods.

        Args:
            request: the POST request.
            return_request_id (int): the return request primary key.

        Returns:
            Response: ``200 OK`` with the request in ``item_received`` status,
                ``400`` if receipt cannot be recorded, or ``404`` when absent.
        """
        return_request = _staff_return_or_404(return_request_id)
        received = _service_error_to_400(record_item_received)(
            return_request=return_request,
            user=request.user,
        )
        serializer = ReturnRequestDetailSerializer(received)
        return Response(serializer.data)


class ReturnRefundView(APIView):
    """Record a staff-sent refund for a received return as staff.

    Staff send the money by hand first, then record it here with a note
    saying how. Requires an ``Idempotency-Key`` and replays the stored
    response on a repeated request with the same key.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    schema = None

    @extend_schema(
        operation_id="admin_return_refund",
        request=ReturnRefundSerializer,
        responses={200: ReturnRequestDetailSerializer},
    )
    def post(self, request, return_request_id):
        """Record the refund for the return request.

        Args:
            request: the POST request carrying an ``Idempotency-Key`` and
                the required ``refund_note``.
            return_request_id (int): the return request primary key.

        Returns:
            Response: ``200 OK`` with the request in ``refunded`` status,
                ``400`` for a business-rule failure, ``404`` when absent, or
                ``409`` when the idempotency key is in progress.
        """
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
            return_request = _staff_return_or_404(return_request_id)
            input_serializer = ReturnRefundSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            refunded = _service_error_to_400(refund_return_request)(
                return_request=return_request,
                refund_note=input_serializer.validated_data["refund_note"],
                user=request.user,
            )
            serializer = ReturnRequestDetailSerializer(refunded)
            store_result(user_pk, key, status.HTTP_200_OK, serializer.data, fingerprint)
            return Response(serializer.data)
        finally:
            release_processing_lock(user_pk, key)


class OrderPreShipmentCancelView(APIView):
    """Cancel a confirmed-but-undelivered order as staff.

    No stock moves and no money moves automatically — the cancellation is a
    status change plus the record of what happened. A staff note is required,
    saying what happened and whether/how a refund was arranged manually.
    Requires an ``Idempotency-Key``.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_order_pre_shipment_cancel",
        request=PreShipmentCancelSerializer,
        responses={200: OrderDetailSerializer},
    )
    def post(self, request, order_id):
        """Cancel the order pre-shipment.

        Args:
            request: the POST request carrying an ``Idempotency-Key``, the
                required ``note``, and the optional refund record.
            order_id (int): the order primary key.

        Returns:
            Response: ``200 OK`` with the cancelled order detail, ``400`` for
                a business-rule failure, ``404`` when absent, or ``409`` when
                the idempotency key is in progress.
        """
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
            order = get_order_for_staff(order_id)
            if order is None:
                raise Http404
            input_serializer = PreShipmentCancelSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            data = input_serializer.validated_data
            cancelled = _service_error_to_400(cancel_confirmed_order)(
                order=order,
                user=request.user,
                note=data["note"],
                refund_note=data.get("refund_note", ""),
                refund_amount=data.get("refund_amount"),
            )
            serializer = OrderDetailSerializer(cancelled)
            store_result(user_pk, key, status.HTTP_200_OK, serializer.data, fingerprint)
            return Response(serializer.data)
        finally:
            release_processing_lock(user_pk, key)
