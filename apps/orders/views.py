"""API views for the orders app.

Two staff-only endpoints (manager/support):

- ``StaffOrderIntakeView`` — create a confirmed order from an assisted
  WhatsApp/email sale, with idempotency protection.
- ``OrderStatusUpdateView`` — advance an order's fulfilment status.

All order mutations go through the order service; the status field is never
written directly in a view.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.orders.selectors import find_orders_by_payment_reference, get_order_for_staff
from apps.orders.serializers import (
    OrderDetailSerializer,
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
