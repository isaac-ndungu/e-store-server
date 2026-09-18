"""API views for the orders app.

Staff-only endpoints (manager/support):

- ``StaffOrderIntakeView`` — create a confirmed order from an assisted
  WhatsApp/email sale, with idempotency protection.
- ``StaffOrderListView`` — paginated order queue with status/phone/source and
  placed-date filtering.
- ``StaffOrderDetailView`` — one order with items and status history.
- ``OrderStatusUpdateView`` — advance an order's fulfilment status.

All order mutations go through the order service; the status field is never
written directly in a view.
"""

from django.utils.dateparse import parse_date, parse_datetime
from django.utils.timezone import is_naive, make_aware
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.orders.models import Order
from apps.orders.selectors import (
    find_orders_by_payment_reference,
    get_order_for_staff,
    list_staff_orders,
)
from apps.orders.serializers import (
    OrderDetailSerializer,
    OrderListSerializer,
    OrderStatusUpdateSerializer,
    StaffOrderIntakeSerializer,
)
from apps.orders.services import apply_staff_status, create_staff_order


class StaffOrderIntakeView(APIView):
    """Create a confirmed order from a staff-assisted sale (staff only).

    Staff enter what the customer agreed over WhatsApp/email, including the
    quoted delivery fee; the order is created already ``confirmed`` with no
    stock movement. Requires an ``Idempotency-Key`` header so a retried
    submit cannot create two orders. The key is bound to the request body:
    reusing it with a different payload is answered with 409 instead of
    replaying the first order.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_intake"

    @extend_schema(
        operation_id="order_staff_intake",
        request=StaffOrderIntakeSerializer,
        responses={201: OrderDetailSerializer},
        tags=["order_intake"],
    )
    def post(self, request):
        """Create the confirmed order.

        Args:
            request: the POST request carrying contact, source, payment, and
                staff-entered lines.

        Returns:
            Response: ``201 Created`` with the order detail (plus a
                ``warnings`` list when the payment reference already appears
                on another order), ``400`` for validation errors,
                ``409`` when the key is already being processed or was used
                with a different payload.
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
        scope = f"u{request.user.pk}"
        fingerprint = request_fingerprint(request)
        cached = read_cached_result(scope, key)
        if cached is not None:
            if payload_conflict(cached, fingerprint):
                return conflicting_key_response()
            return Response(cached["data"], status=cached["status"])
        if not acquire_processing_lock(scope, key):
            return Response(
                {
                    "detail": "A request with this Idempotency-Key is already in progress."
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            input_serializer = StaffOrderIntakeSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            data = input_serializer.validated_data
            order = _service_error_to_400(create_staff_order)(
                staff_user=request.user,
                phone=data["phone"],
                lines=data["items"],
                order_source=data["order_source"],
                payment_method=data["payment_method"],
                payment_reference=data.get("payment_reference", ""),
                email=data.get("email", ""),
                notes=data.get("notes", ""),
                delivery_area_id=data.get("delivery_area_id"),
                delivery_fee=data.get("delivery_fee"),
                shipping_address_id=data.get("shipping_address_id"),
                inquiry_id=data.get("inquiry_id"),
            )
            response_data = OrderDetailSerializer(order).data
            duplicates = find_orders_by_payment_reference(
                order.payment_reference, exclude_pk=order.pk
            )
            if duplicates:
                response_data["warnings"] = [
                    {
                        "code": "duplicate_payment_reference",
                        "detail": (
                            "This payment reference is already used on "
                            f"order {dup['id']} — confirm it is not a "
                            "data-entry mistake."
                        ),
                        "order_id": dup["id"],
                    }
                    for dup in duplicates
                ]
            store_result(
                scope, key, status.HTTP_201_CREATED, response_data, fingerprint
            )
            return Response(response_data, status=status.HTTP_201_CREATED)
        finally:
            release_processing_lock(scope, key)


class StaffOrderListView(APIView):
    """List orders for staff, newest-first with optional filters.

    Staff-only (manager/support). Supported query params: ``status`` (exact,
    must be a known order status), ``phone`` (case-insensitive substring),
    ``order_source`` (exact, must be a known source), ``from`` / ``to``
    (ISO date or datetime bounding ``placed_at``). Invalid values are
    answered with 400; unknown params are ignored.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    @extend_schema(
        operation_id="order_staff_list",
        responses={200: OrderListSerializer(many=True)},
        tags=["order_intake"],
    )
    def get(self, request):
        """Return the paginated, optionally filtered order queue.

        Args:
            request: the GET request with optional filter params.

        Returns:
            Response: the paginated order list, or ``400`` for an invalid
                filter value.
        """
        params = request.query_params
        status_value = params.get("status") or None
        if status_value and status_value not in dict(Order.STATUS_CHOICES):
            return Response(
                {"status": "Unknown order status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        source_value = params.get("order_source") or None
        if source_value and source_value not in dict(Order.ORDER_SOURCE_CHOICES):
            return Response(
                {"order_source": "Unknown order source."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            placed_from = _parse_placed_bound(params.get("from"))
            placed_to = _parse_placed_bound(params.get("to"), end_of_day=True)
        except ValueError:
            return Response(
                {"detail": "from/to must be an ISO date or datetime."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = list_staff_orders(
            status=status_value,
            phone=params.get("phone") or None,
            order_source=source_value,
            placed_from=placed_from,
            placed_to=placed_to,
        )
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = OrderListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class StaffOrderDetailView(APIView):
    """Retrieve one order for staff with items and status history."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    @extend_schema(
        operation_id="order_staff_detail",
        responses={200: OrderDetailSerializer},
        tags=["order_intake"],
    )
    def get(self, request, order_id):
        """Return the matching order.

        Args:
            request: the GET request.
            order_id (int): the order id.

        Returns:
            Response: the order detail, or ``404`` when no order matches.

        Raises:
            Http404: when no order matches the id.
        """
        from django.http import Http404

        order = get_order_for_staff(order_id)
        if order is None:
            raise Http404
        return Response(OrderDetailSerializer(order).data)


def _parse_placed_bound(value, end_of_day=False):
    """Parse a ``from``/``to`` filter into an aware datetime.

    Accepts a full ISO datetime or a plain ISO date (midnight, or
    end-of-day when ``end_of_day`` is set so a ``to`` date stays
    inclusive). Returns None for a missing value.

    Args:
        value (str | None): the raw query param.
        end_of_day (bool): snap plain dates to 23:59:59.999999.

    Returns:
        datetime | None: the aware bound, or None when unset.

    Raises:
        ValueError: when the value parses as neither.
    """
    from datetime import datetime, time

    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        day = parse_date(value)
        if day is None:
            raise ValueError(f"Unparseable date bound: {value!r}")
        parsed = datetime.combine(
            day, time.max if end_of_day else time.min
        )
    if is_naive(parsed):
        parsed = make_aware(parsed)
    return parsed


class OrderStatusUpdateView(APIView):
    """Advance an order's fulfilment status as a staff user.

    Staff-only, restricted to fulfilment roles (manager/support). The acting
    staff member names a target status and optional note; the shared service
    validates the transition against the allowed graph and writes a
    status-history row alongside it.
    """

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_write"

    @extend_schema(
        operation_id="order_status_update",
        request=OrderStatusUpdateSerializer,
        responses={200: OrderDetailSerializer},
    )
    def post(self, request, order_id):
        """Transition an order to the requested status.

        Args:
            request: the POST request carrying ``to_status`` and an optional
                ``note``.
            order_id (int): the order id.

        Returns:
            Response: ``200 OK`` with the updated order, ``400`` for an
                illegal transition, or ``404`` when the order does not exist.
        """
        from django.http import Http404

        order = get_order_for_staff(order_id)
        if order is None:
            raise Http404
        input_serializer = OrderStatusUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        updated = _service_error_to_400(apply_staff_status)(
            order,
            data["to_status"],
            changed_by=request.user,
            note=data.get("note", ""),
        )
        serializer = OrderDetailSerializer(updated)
        return Response(serializer.data)
