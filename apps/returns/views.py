"""API views for the returns app.

Return endpoints split by caller type:

- Customer endpoints sit under an order's ``return-requests`` sub-resource
  and reuse the orders app's shared resolver, so both an authenticated owner
  and a guest holding the order's lookup token can only reach their own
  orders' returns — a missing or foreign reference resolves to 404, never 403.
- Staff endpoints under ``returns/`` are gated by the manager/support role and
  operate across all orders. The money-moving staff actions (refund, and the
  pre-shipment cancellation that refunds an order) require an
  ``Idempotency-Key`` so a retried request cannot fire a duplicate transfer.

All mutations go through the returns service; no view writes a status or
money field directly.
"""

from django.http import Http404
from rest_framework import permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.orders.selectors import get_order_for_staff
from apps.orders.views import _resolve_order
from apps.payments.daraja import DarajaError
from apps.returns.selectors import (
    get_return_request_for_order,
    get_return_request_for_staff,
    list_all_return_requests,
    list_return_requests_for_order,
)
from apps.returns.serializers import (
    PreShipmentCancelSerializer,
    ReturnApproveSerializer,
    ReturnRejectSerializer,
    ReturnRequestCreateSerializer,
    ReturnRequestCustomerDetailSerializer,
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


class OrderReturnRequestListCreateView(APIView):
    """List or open return requests for a caller's delivered order."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_write"

    def get(self, request, order_ref, *_args, **_kwargs):
        """Return the order's return requests, newest first.

        Args:
            request: the GET request.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: the paginated return-request list for the order.
        """
        order = _resolve_order(request, order_ref)
        requests = list_return_requests_for_order(order)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(requests, request)
        serializer = ReturnRequestListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, order_ref, *_args, **_kwargs):
        """Open a return request against the caller's delivered order.

        Args:
            request: the POST request carrying the return payload.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: ``201 Created`` with the created request detail, ``400``
                for a validation/business-rule failure, or ``404`` when the
                order is not the caller's.
        """
        order = _resolve_order(request, order_ref)
        input_serializer = ReturnRequestCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        return_request = _service_error_to_400(create_return_request)(
            order=order,
            order_item_id=data.get("order_item_id"),
            reason=data["reason"],
            requested_resolution=data["requested_resolution"],
            user=request.user if request.user.is_authenticated else None,
        )
        serializer = ReturnRequestCustomerDetailSerializer(return_request)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class OrderReturnRequestDetailView(APIView):
    """Retrieve a single return request on a caller's order."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    def get(self, request, order_ref, return_request_id):
        """Return the matching return request with full detail.

        Args:
            request: the GET request.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).
            return_request_id (int): the return request primary key.

        Returns:
            Response: the full return request detail.

        Raises:
            Http404: when the order is not the caller's or the request does
                not belong to it.
        """
        order = _resolve_order(request, order_ref)
        return_request = get_return_request_for_order(order, return_request_id)
        if return_request is None:
            raise Http404
        serializer = ReturnRequestCustomerDetailSerializer(return_request)
        return Response(serializer.data)


class ReturnRequestStaffListView(APIView):
    """List every return request for managers and support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

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

    def post(self, request, return_request_id):
        """Receive the returned goods and restock the line.

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
    """Initiate a refund for a received return as staff.

    Money moves on this action, so it requires an ``Idempotency-Key`` and
    replays the stored response on a repeated request with the same key. An
    M-Pesa B2C payout completes only once Safaricom confirms it; the return
    request then moves to ``refunded`` via the callback.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, return_request_id):
        """Refund the return request.

        Args:
            request: the POST request carrying an ``Idempotency-Key``.
            return_request_id (int): the return request primary key.

        Returns:
            Response: ``200 OK`` with the request (in ``refunded`` status for
                a card reversal, or ``item_received`` awaiting the B2C
                callback), ``400`` for a business-rule failure, ``404`` when
                absent, or ``409`` when the idempotency key is in progress.
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
            return_request = _staff_return_or_404(return_request_id)
            refunded = _service_error_to_400(refund_return_request)(
                return_request=return_request,
                user=request.user,
            )
            serializer = ReturnRequestDetailSerializer(refunded)
            store_result(user_pk, key, status.HTTP_200_OK, serializer.data)
            return Response(serializer.data)
        except DarajaError:
            return Response(
                {
                    "detail": (
                        "The refund provider is unavailable right now; the "
                        "request was not refunded. Please retry shortly."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        finally:
            release_processing_lock(user_pk, key)


class OrderPreShipmentCancelView(APIView):
    """Cancel a confirmed-but-undelivered order and refund it as staff.

    Money moves on this action, so it requires an ``Idempotency-Key``. Stock
    is restocked and whatever was collected pre-delivery is refunded (M-Pesa,
    card), then the order moves to ``cancelled``.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, order_id):
        """Cancel the order pre-shipment.

        Args:
            request: the POST request carrying an ``Idempotency-Key`` and an
                optional ``note``.
            order_id (int): the order primary key.

        Returns:
            Response: ``200 OK`` with the cancelled order detail, ``400`` for
                a business-rule failure, ``404`` when absent, or ``409`` when
                the idempotency key is in progress.
        """
        from apps.core.idempotency import (
            acquire_processing_lock,
            read_cached_result,
            release_processing_lock,
            require_idempotency_key,
            store_result,
        )
        from apps.orders.serializers import OrderDetailSerializer

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
            order = get_order_for_staff(order_id)
            if order is None:
                raise Http404
            input_serializer = PreShipmentCancelSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            cancelled = _service_error_to_400(cancel_confirmed_order)(
                order=order,
                user=request.user,
                note=input_serializer.validated_data.get("note", ""),
            )
            serializer = OrderDetailSerializer(cancelled)
            store_result(user_pk, key, status.HTTP_200_OK, serializer.data)
            return Response(serializer.data)
        except DarajaError:
            return Response(
                {
                    "detail": (
                        "The refund provider is unavailable right now; the "
                        "order was not cancelled. Please retry shortly."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        finally:
            release_processing_lock(user_pk, key)
