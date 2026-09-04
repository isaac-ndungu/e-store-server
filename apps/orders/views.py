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

from apps.accounts.services import validate_phone_number
from apps.cart.services import get_or_create_cart
from apps.orders.selectors import (
    get_order_by_phone,
    get_order_for_user,
    list_orders_for_user,
)
from apps.orders.serializers import (
    OrderCreateSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    OrderStatusHistorySerializer,
    OrderVerificationSerializer,
    OTPVerifySerializer,
)
from apps.orders.services import (
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


def _resolve_phone_from_query(request):
    """Return a validated E.164 phone from the request query, or ``None``.

    Args:
        request: the incoming HTTP request.

    Returns:
        str | None: the validated phone number, or None when absent/invalid.
    """
    phone = request.query_params.get("phone", "")
    if not phone:
        return None
    try:
        return validate_phone_number(phone)
    except DjangoValidationError:
        return None


def _resolve_order(request, order_id):
    """Return the caller's order or raise HTTP 404.

    An authenticated caller addresses their own order. A guest addresses an
    order only when the request's ``phone`` query parameter matches the
    order's contact number, so a caller cannot enumerate another person's
    orders by guessing ids. Both a missing id and another caller's id raise
    404.

    Args:
        request: the incoming HTTP request.
        order_id (int): the order id.

    Returns:
        Order: the resolved order.
    """
    if request.user.is_authenticated:
        order = get_order_for_user(request.user, order_id)
    else:
        phone = _resolve_phone_from_query(request)
        if phone:
            order = get_order_by_phone(order_id, phone)
        else:
            order = None
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

            otp_required = requires_otp_for_payment(order)
            if otp_required:
                resend_order_otp(order)

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

    def get(self, request, order_id):
        """Retrieve the owned order with full detail.

        Args:
            request: the GET request.
            order_id (int): the order id.

        Returns:
            Response: the full order detail.
        """
        order = _resolve_order(request, order_id)
        serializer = OrderDetailSerializer(order)
        return Response(serializer.data)

    def delete(self, request, order_id):
        """Cancel a pending order, releasing held stock.

        Args:
            request: the DELETE request.
            order_id (int): the order id.

        Returns:
            Response: ``200 OK`` with the cancelled order, or ``400`` if the
                order is not pending.
        """
        order = _resolve_order(request, order_id)
        cancelled = _service_error_to_400(cancel_pending_order)(
            order,
            user=request.user if request.user.is_authenticated else None,
        )
        serializer = OrderDetailSerializer(cancelled)
        return Response(serializer.data)


class OrderVerifyOTPView(APIView):
    """Verify a COD order with a submitted one-time password."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_verify"

    def post(self, request, order_id):
        """Verify the order's OTP and confirm the order on success.

        Args:
            request: the POST request carrying ``otp_code``.
            order_id (int): the order id.

        Returns:
            Response: ``200 OK`` with the confirmed order on success, ``400``
                for an invalid/expired/maxed-out code, or ``404`` when the
                order is not owned by the caller.
        """
        order = _resolve_order(request, order_id)
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

    def post(self, request, order_id):
        """Re-send and reset the OTP for a COD order.

        Args:
            request: the POST request.
            order_id (int): the order id.

        Returns:
            Response: ``200 OK`` with the updated verification state, ``400``
                for a non-COD or non-pending order, or ``404`` when the order
                is not owned by the caller.
        """
        order = _resolve_order(request, order_id)
        verification = _service_error_to_400(resend_order_otp)(order)
        serializer = OrderVerificationSerializer(verification)
        return Response(serializer.data)


class OrderStatusHistoryView(APIView):
    """Return the status audit trail for a caller's order."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    def get(self, request, order_id):
        """Return the order's status history.

        Args:
            request: the GET request.
            order_id (int): the order id.

        Returns:
            Response: the list of status transitions.
        """
        order = _resolve_order(request, order_id)
        serializer = OrderStatusHistorySerializer(order.status_history, many=True)
        return Response(serializer.data)
