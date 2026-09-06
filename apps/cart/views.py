"""API views for the cart app.

The cart is always addressed as a single-object resource: ``GET /cart/``
returns the current user's (or guest's) active cart with computed totals.
Cart mutations are ``POST`` to sub-endpoints (``/cart/items/``,
``/cart/apply-coupon/``, etc.) to keep the URL scheme clean.

Permission model:
- Cart endpoints are reachable by authenticated users (JWT) and by anonymous
  guests.  Guest carts are keyed by Django's own server-issued session
  (``request.session.session_key``) carried in the ``HttpOnly`` session
  cookie — never by a client-invented header value, which would be weak and
  forgeable.  A caller can only ever address their own cart.
- ``WishlistView`` and ``WishlistItemDetailView`` require authentication —
  the wishlist is tied to an account.
- Ownership is enforced at the service/view layer: a caller can only address
  their own cart, and wishlist mutations are scoped to the authenticated user.
"""

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.cart.selectors import get_cart_for_session, get_wishlist_for_user
from apps.cart.serializers import (
    CartItemQuantitySerializer,
    CartItemWriteSerializer,
    CartSummarySerializer,
    CouponApplySerializer,
    CouponResultSerializer,
    WishlistAddSerializer,
    WishlistItemSerializer,
)
from apps.cart.services import (
    add_item,
    add_to_wishlist,
    apply_coupon,
    compute_cart_totals,
    get_or_create_cart,
    remove_coupon,
    remove_from_wishlist,
    remove_item,
    update_item_quantity,
)
from apps.core.api import service_error_to_400 as _service_error_to_400


def _ensure_guest_session(request):
    """Return the request's Django session key, creating a session if needed.

    The session key identifies the guest cart.  Django generates a
    server-random key and stores the session server-side, so the guest cart
    cannot be reached by guessing a client-supplied value.

    Args:
        request: the incoming HTTP request.

    Returns:
        str: the session key.
    """
    if request.session.session_key is None:
        request.session.create()
    return request.session.session_key


def _resolve_cart(request):
    """Resolve the active cart for the request's user or guest.

    For authenticated users the cart is user-scoped.  For guests the cart is
    keyed by the request's Django session.  A session (and therefore a cart)
    is always created for the caller, so this never returns ``None``.

    Args:
        request: the incoming HTTP request.

    Returns:
        Cart: the caller's active cart.
    """
    if request.user.is_authenticated:
        return get_or_create_cart(user=request.user)
    session_key = _ensure_guest_session(request)
    cart = get_cart_for_session(session_key)
    if cart is None:
        cart = get_or_create_cart(session_key=session_key)
    return cart


# Cart endpoints


class CartView(APIView):
    """Return the active cart with computed totals.

    Supports both authenticated users and anonymous guests (keyed by Django's
    server-side session).  The response includes server-computed effective
    prices, discount breakdowns, and VAT — all money values are strings for
    exact representation.

    GET returns the current cart (creating one if needed).
    DELETE empties the cart by removing all items and clearing the coupon.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request):
        """Return the active cart with priced line items and totals.

        Args:
            request: the GET request.

        Returns:
            Response: the full cart summary.
        """
        cart = _resolve_cart(request)
        totals = compute_cart_totals(cart)
        serializer = CartSummarySerializer(
            {
                "id": cart.pk,
                "user": cart.user_id,
                "session_key": cart.session_key,
                "coupon_code": cart.coupon.code if cart.coupon_id else None,
                "created_at": cart.created_at,
                "updated_at": cart.updated_at,
                **totals,
            }
        )
        return Response(serializer.data)

    def delete(self, request):
        """Empty the cart by removing all items and clearing the coupon.

        Args:
            request: the DELETE request.

        Returns:
            Response: ``204 No Content`` on success.
        """
        cart = _resolve_cart(request)
        cart.items.all().delete()
        if cart.coupon_id is not None:
            cart.coupon = None
            cart.save(update_fields=["coupon", "updated_at"])
        else:
            cart.save(update_fields=["updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class CartItemsView(APIView):
    """Add items to the cart.

    POST with ``variant_id`` or ``bundle_id`` and ``quantity``.  Duplicate
    lines are merged by summing the quantity.  Validates stock availability
    for variants and bundle/variant active status.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def post(self, request):
        """Add an item to the active cart.

        Args:
            request: the POST request carrying ``variant_id`` or
                ``bundle_id`` and optionally ``quantity``.

        Returns:
            Response: ``201 Created`` with the cart item id, or ``400``
                for invalid input / stock errors.
        """
        cart = _resolve_cart(request)
        input_serializer = CartItemWriteSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        item = _service_error_to_400(add_item)(
            cart,
            variant_id=data.get("variant_id"),
            bundle_id=data.get("bundle_id"),
            quantity=data["quantity"],
        )
        return Response({"item_id": item.pk}, status=status.HTTP_201_CREATED)


class CartItemDetailView(APIView):
    """Update the quantity or remove a specific cart item.

    PATCH with ``quantity`` to change the quantity.
    DELETE to remove the item entirely.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def patch(self, request, item_id):
        """Update the quantity of a cart item.

        Args:
            request: the PATCH request carrying ``quantity``.
            item_id (int): the cart-item id.

        Returns:
            Response: ``200 OK`` with the updated item id, or ``204`` if
                the item was removed (quantity <= 0).
        """
        cart = _resolve_cart(request)
        input_serializer = CartItemQuantitySerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(update_item_quantity)(
            cart, item_id, input_serializer.validated_data["quantity"]
        )
        if updated is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response({"item_id": updated.pk})

    def delete(self, request, item_id):
        """Remove a cart item.

        Args:
            request: the DELETE request.
            item_id (int): the cart-item id.

        Returns:
            Response: ``204 No Content`` on success.
        """
        cart = _resolve_cart(request)
        _service_error_to_400(remove_item)(cart, item_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CartApplyCouponView(APIView):
    """Apply a coupon code to the active cart."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "coupon_validate"

    def post(self, request):
        """Apply a coupon code to the active cart.

        Args:
            request: the POST request carrying ``code``.

        Returns:
            Response: ``200 OK`` with the validation result, or ``400``
                if the coupon is invalid.
        """
        cart = _resolve_cart(request)
        input_serializer = CouponApplySerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        user = request.user if request.user.is_authenticated else None
        result = _service_error_to_400(apply_coupon)(
            cart, input_serializer.validated_data["code"], user=user
        )
        output_serializer = CouponResultSerializer(result)
        return Response(output_serializer.data)


class CartRemoveCouponView(APIView):
    """Remove the coupon from the active cart."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "coupon_validate"

    def delete(self, request):
        """Remove the coupon from the active cart.

        Args:
            request: the DELETE request.

        Returns:
            Response: ``204 No Content`` on success.
        """
        cart = _resolve_cart(request)
        remove_coupon(cart)
        return Response(status=status.HTTP_204_NO_CONTENT)


# Wishlist endpoints


class WishlistView(APIView):
    """List wishlist items or add a new one.

    Requires authentication — the wishlist is tied to an account.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request):
        """Return the authenticated user's wishlist.

        Args:
            request: the GET request.

        Returns:
            Response: the wishlist items with product details.
        """
        items = get_wishlist_for_user(request.user)
        serializer = WishlistItemSerializer(items, many=True)
        return Response(serializer.data)

    def post(self, request):
        """Add a product to the authenticated user's wishlist.

        Args:
            request: the POST request carrying ``product_id``.

        Returns:
            Response: ``201 Created`` with the wishlist item, or ``400``
                if the product does not exist.
        """
        input_serializer = WishlistAddSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        item = _service_error_to_400(add_to_wishlist)(
            request.user, input_serializer.validated_data["product_id"]
        )
        output_serializer = WishlistItemSerializer(item)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class WishlistItemDetailView(APIView):
    """Remove a product from the wishlist.

    Requires authentication.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def delete(self, request, product_id):
        """Remove a product from the authenticated user's wishlist.

        Args:
            request: the DELETE request.
            product_id (int): the product id to remove.

        Returns:
            Response: ``204 No Content`` on success.
        """
        _service_error_to_400(remove_from_wishlist)(request.user, product_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
