"""API views for the orders app.

Order endpoints split by caller type:

- ``OrderListCreateView`` — authenticated users list their own orders and
  place new ones from their cart. A guest (no auth) can place an order too;
  order creation is authenticated-or-guest and the returned order id lets the
  caller poll status and verify a COD code.
- ``OrderDetailView`` — an authenticated user retrieves/cancels one of their
  own orders; a guest does so only when the id matches the contact phone
  carried in the request (normalized E.164).
- The OTP endpoints verify or re-send a COD code using the same ownership
  resolution.

All order mutations go through the order service; the status field is never
written directly in a view. Ownership/IDOR is enforced through a shared
resolver that returns 404 (not 403) for a missing or another user's order.
"""

from functools import wraps

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.cart.services import get_or_create_cart
from apps.orders.selectors import (
    get_order_by_token,
    get_order_for_staff,
    get_order_for_user,
    list_orders_for_user,
)
from apps.orders.serializers import (
    CancelOrderSerializer,
    OrderCreateSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    OrderStatusHistorySerializer,
    OrderStatusUpdateSerializer,
    OrderVerificationSerializer,
    OTPVerifySerializer,
)
from apps.orders.services import (
    apply_staff_status,
    cancel_pending_order,
    create_order_from_cart,
    requires_otp_for_payment,
    resend_order_otp,
    verify_order_otp,
)


def _service_error_to_400(mutation):
    """Convert a service-layer validation error into a DRF 400 response.

    Orders services raise Django's ``ValidationError``; DRF only translates
    the ``rest_framework`` variant, so a mutation that raises for a business
    rule would otherwise surface as a 500.

    Args:
        mutation (Callable): the service function to invoke.

    Returns:
        Callable: a wrapper that raises DRF's ``ValidationError`` on a
            service validation failure.
    """

    @wraps(mutation)
    def wrapper(*args, **kwargs):
        try:
            return mutation(*args, **kwargs)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.messages) from exc

    return wrapper


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


def _resolve_cart(request):
    """Return the caller's active cart, creating one if needed.

    Args:
        request: the incoming HTTP request.

    Returns:
        Cart: the caller's cart.
    """
    if request.user.is_authenticated:
        return get_or_create_cart(user=request.user)
    session_key = _ensure_guest_session(request)
    return get_or_create_cart(session_key=session_key)


def _resolve_order(request, order_ref):
    """Return the caller's order or raise HTTP 404.

    An authenticated caller addresses their own order by id. A guest addresses
    an order by its unguessable ``lookup_token`` (returned at creation) — no
    id guessing and no phone number in the request. Both a missing reference
    and another caller's reference raise 404, so nothing reveals whether an
    order exists.

    Args:
        request: the incoming HTTP request.
        order_ref (str): an order id (authenticated) or lookup token (guest).

    Returns:
        Order: the resolved order.

    Raises:
        HTTPError: ``404`` when the order is absent or not the caller's.
    """
    if request.user.is_authenticated:
        try:
            order_id = int(order_ref)
        except TypeError, ValueError:
            order = None
        else:
            order = get_order_for_user(request.user, order_id)
    else:
        order = get_order_by_token(order_ref)
    if order is None:
        raise Http404
    return order


def _paginated_orders(request, orders):
    """Return a paginated orders response payload.

    Args:
        request: the incoming GET request.
        orders (QuerySet): the orders queryset.

    Returns:
        dict: the paginated response body.
    """
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(orders, request)
    serializer = OrderListSerializer(page, many=True)
    return {
        "count": paginator.page.paginator.count,
        "next": paginator.get_next_link(),
        "previous": paginator.get_previous_link(),
        "results": serializer.data,
    }


class OrderListCreateView(APIView):
    """List the caller's orders or place a new order from their cart.

    GET returns the authenticated user's own orders, paginated. Anonymous
    callers get an empty list — guests have no user row to list orders by.
    POST places an order from the caller's cart (authenticated or guest) and
    requires an ``Idempotency-Key`` header so a repeated tap cannot create
    two orders.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_write"

    def get(self, request):
        """Return the authenticated caller's paginated orders.

        Args:
            request: the GET request.

        Returns:
            Response: the caller's orders, paginated.
        """
        if not request.user.is_authenticated:
            return Response({"count": 0, "next": None, "previous": None, "results": []})
        orders = list_orders_for_user(request.user)
        payload = _paginated_orders(request, orders)
        return Response(payload)

    def post(self, request):
        """Place an order from the caller's cart with idempotency protection.

        Requires the ``Idempotency-Key`` header. The order is created in
        ``pending`` status with stock reserved; for COD orders an SMS OTP is
        sent to ``order.phone`` to verify the number before confirmation. The
        response includes a ``requires_otp`` flag so the storefront knows
        whether to prompt for a code.

        Args:
            request: the POST request carrying the order payload.

        Returns:
            Response: ``201 Created`` with the created order detail, ``400``
                for validation/stock errors, or ``409`` when the same
                idempotency key is already being processed.
        """
        from apps.core.idempotency import (
            acquire_processing_lock,
            read_cached_result,
            release_processing_lock,
            require_idempotency_key,
            store_result,
        )

        key = require_idempotency_key(request)
        user_pk = request.user.pk if request.user.is_authenticated else 0
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
            cart = _resolve_cart(request)
            input_serializer = OrderCreateSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            data = input_serializer.validated_data

            order = _service_error_to_400(create_order_from_cart)(
                cart=cart,
                user=request.user if request.user.is_authenticated else None,
                phone=data["phone"],
                shipping_address=(
                    _load_owned_address(request, data["shipping_address_id"])
                    if data.get("shipping_address_id")
                    else None
                ),
                delivery_zone_id=data.get("delivery_zone_id"),
                payment_method=data["payment_method"],
                email=data.get("email", ""),
                notes=data.get("notes", ""),
            )

            from apps.orders.payments import (
                initiate_payment,
                is_payment_method_available,
            )

            otp_required = requires_otp_for_payment(order)
            if otp_required:
                resend_order_otp(order)
            elif is_payment_method_available(order.payment_method):
                initiate_payment(order)

            serializer = OrderDetailSerializer(order)
            response_data = {
                **serializer.data,
                "requires_otp": otp_required,
            }
            store_result(user_pk, key, status.HTTP_201_CREATED, response_data)
            return Response(response_data, status=status.HTTP_201_CREATED)
        finally:
            release_processing_lock(user_pk, key)


def _load_owned_address(request, address_id):
    """Return a stored address that belongs to the caller.

    An authenticated caller's address must be their own; an address id the
    caller does not own raises a 404 (indistinguishable from a missing
    address). Guests have no stored addresses and return None.

    Args:
        request: the incoming HTTP request.
        address_id (int): the address id.

    Returns:
        Address | None: the owned address, or None for a guest.
    """
    if not request.user.is_authenticated:
        return None
    from django.shortcuts import get_object_or_404

    from apps.accounts.models import Address

    return get_object_or_404(Address, pk=address_id, user=request.user)


class OrderDetailView(APIView):
    """Retrieve or cancel a single order owned by the caller."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    def get(self, request, order_ref):
        """Retrieve the owned order with full detail.

        Args:
            request: the GET request.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: the full order detail.
        """
        order = _resolve_order(request, order_ref)
        serializer = OrderDetailSerializer(order)
        return Response(serializer.data)

    def delete(self, request, order_ref):
        """Cancel a pending order, releasing held stock.

        Args:
            request: the DELETE request.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: ``200 OK`` with the cancelled order, or ``400`` if the
                order is not pending.
        """
        order = _resolve_order(request, order_ref)
        cancelled = _service_error_to_400(cancel_pending_order)(
            order,
            user=request.user if request.user.is_authenticated else None,
        )
        serializer = OrderDetailSerializer(cancelled)
        return Response(serializer.data)


class OrderCancelView(APIView):
    """Cancel a pending order via an explicit cancel sub-resource.

    Mirrors the DELETE on the detail view but as a POST under ``/cancel/``,
    protected by an ``Idempotency-Key`` so a retried tap cannot re-process a
    cancellation. Cancellation is naturally idempotent (only active
    reservations are released and a non-pending order is rejected), but
    requiring the key keeps client retry semantics predictable.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_write"

    def post(self, request, order_ref):
        """Cancel the caller's pending order.

        Args:
            request: the POST request carrying an ``Idempotency-Key``.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: ``200 OK`` with the cancelled order, or ``400`` for a
                non-pending order or an invalid idempotency key, or ``404``
                when the order is not owned by the caller.
        """
        from apps.core.idempotency import (
            acquire_processing_lock,
            read_cached_result,
            release_processing_lock,
            require_idempotency_key,
            store_result,
        )

        key = require_idempotency_key(request)
        user_pk = request.user.pk if request.user.is_authenticated else 0
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
            order = _resolve_order(request, order_ref)
            input_serializer = CancelOrderSerializer(data=request.data)
            input_serializer.is_valid(raise_exception=True)
            cancelled = _service_error_to_400(cancel_pending_order)(
                order,
                user=request.user if request.user.is_authenticated else None,
                note=input_serializer.validated_data.get("note", ""),
            )
            serializer = OrderDetailSerializer(cancelled)
            store_result(user_pk, key, status.HTTP_200_OK, serializer.data)
            return Response(serializer.data)
        finally:
            release_processing_lock(user_pk, key)


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


class OrderVerifyOTPView(APIView):
    """Verify a COD order with a submitted one-time password."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_verify"

    def post(self, request, order_ref):
        """Verify the order's OTP and confirm the order on success.

        Args:
            request: the POST request carrying ``otp_code``.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: ``200 OK`` with the confirmed order on success, ``400``
                for an invalid/expired/maxed-out code, or ``404`` when the
                order is not owned by the caller.
        """
        order = _resolve_order(request, order_ref)
        input_serializer = OTPVerifySerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        order, _verified = _service_error_to_400(verify_order_otp)(
            order, input_serializer.validated_data["otp_code"]
        )
        serializer = OrderDetailSerializer(order)
        return Response(serializer.data)


class OrderResendOTPView(APIView):
    """Re-send the COD verification code."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_otp_resend"

    def post(self, request, order_ref):
        """Re-send and reset the OTP for a COD order.

        Args:
            request: the POST request.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: ``200 OK`` with the updated verification state, ``400``
                for a non-COD or non-pending order, or ``404`` when the order
                is not owned by the caller.
        """
        order = _resolve_order(request, order_ref)
        verification = _service_error_to_400(resend_order_otp)(order)
        serializer = OrderVerificationSerializer(verification)
        return Response(serializer.data)


class OrderStatusHistoryView(APIView):
    """Return the status audit trail for a caller's order."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    def get(self, request, order_ref):
        """Return the order's status history.

        Args:
            request: the GET request.
            order_ref (str): the order id (authenticated) or lookup token
                (guest).

        Returns:
            Response: the list of status transitions.
        """
        order = _resolve_order(request, order_ref)
        serializer = OrderStatusHistorySerializer(order.status_history, many=True)
        return Response(serializer.data)
