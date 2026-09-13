"""API views for the cart app.

The cart is deliberately public (``AllowAny``): there are no customer
accounts, so possession of the ``estore_cart`` cookie is the cart's identity.
Every response refreshes the cookie expiry. Views stay thin — line mutations
live in the services layer and pricing in the selectors.
"""

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.cart import services
from apps.cart.selectors import (
    CART_COOKIE_MAX_AGE,
    CART_COOKIE_NAME,
    get_or_create_cart,
    price_cart_lines,
)
from apps.cart.serializers import (
    CartItemAddSerializer,
    CartItemUpdateSerializer,
    CartSerializer,
)
from apps.core.api import service_error_to_400 as _service_error_to_400


def _set_cart_cookie(response, request, cart):
    """Stamp the visitor's cart cookie onto a response.

    Args:
        response (Response): the outgoing response.
        request: the incoming request.
        cart (Cart): the visitor's cart.

    Returns:
        Response: the same response with the cookie set.
    """
    response.set_cookie(
        CART_COOKIE_NAME,
        str(cart.anonymous_id),
        max_age=CART_COOKIE_MAX_AGE,
        httponly=True,
        secure=request.is_secure() or not settings.DEBUG,
        samesite="Lax",
    )
    return response


def _cart_payload(cart):
    """Build the serialized cart body with server-computed prices.

    Args:
        cart (Cart): the visitor's cart with items prefetched.

    Returns:
        dict: the validated cart read shape.
    """
    priced_lines, subtotal = price_cart_lines(cart)
    flat_lines = [
        {
            "id": entry["item"].pk,
            "variant": entry["variant"],
            "bundle": entry["bundle"],
            "quantity": entry["quantity"],
            "unit_price": entry["unit_price"],
            "line_total": entry["line_total"],
            "item": entry["item"],
        }
        for entry in priced_lines
    ]
    payload = {
        "id": cart.pk,
        "item_count": sum(entry["quantity"] for entry in priced_lines),
        "subtotal": subtotal,
        "updated_at": cart.updated_at,
        "items": flat_lines,
    }
    return CartSerializer(payload).data


class CartDetailView(APIView):
    """Read the visitor's cart (public guest cart)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "cart_read"

    @extend_schema(
        operation_id="cart_retrieve",
        responses={200: CartSerializer},
        tags=["cart"],
    )
    def get(self, request):
        """Return the cart lines with server-computed prices.

        Args:
            request: the GET request carrying the cart cookie.

        Returns:
            Response: ``200 OK`` with the cart body.
        """
        cart, _ = get_or_create_cart(request.COOKIES)
        return _set_cart_cookie(Response(_cart_payload(cart)), request, cart)


class CartItemCreateView(APIView):
    """Add a line to the visitor's cart (public guest cart)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "cart_write"

    @extend_schema(
        operation_id="cart_item_add",
        request=CartItemAddSerializer,
        responses={201: CartSerializer},
        tags=["cart"],
    )
    def post(self, request):
        """Add a variant line, merging with an identical line if present.

        Args:
            request: the POST request with ``variant_id`` + quantity.

        Returns:
            Response: ``201 Created`` with the updated cart body.
        """
        input_serializer = CartItemAddSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        cart, _ = get_or_create_cart(request.COOKIES)
        _service_error_to_400(services.add_item)(
            cart, **input_serializer.validated_data
        )
        cart.refresh_from_db()
        return _set_cart_cookie(
            Response(_cart_payload(cart), status=201), request, cart
        )


class CartItemDetailView(APIView):
    """Change or remove one line of the visitor's cart (public guest cart)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "cart_write"

    @extend_schema(
        operation_id="cart_item_update",
        request=CartItemUpdateSerializer,
        responses={200: CartSerializer},
        tags=["cart"],
    )
    def patch(self, request, item_id):
        """Set a cart line's quantity.

        Args:
            request: the PATCH request with the new quantity.
            item_id (int): the cart line pk.

        Returns:
            Response: ``200 OK`` with the updated cart body.
        """
        input_serializer = CartItemUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        cart, _ = get_or_create_cart(request.COOKIES)
        _service_error_to_400(services.update_item)(
            cart, item_id, **input_serializer.validated_data
        )
        cart.refresh_from_db()
        return _set_cart_cookie(Response(_cart_payload(cart)), request, cart)

    @extend_schema(
        operation_id="cart_item_remove",
        responses={200: CartSerializer},
        tags=["cart"],
    )
    def delete(self, request, item_id):
        """Remove a line from the cart.

        Args:
            request: the DELETE request.
            item_id (int): the cart line pk.

        Returns:
            Response: ``200 OK`` with the updated cart body.
        """
        cart, _ = get_or_create_cart(request.COOKIES)
        _service_error_to_400(services.remove_item)(cart, item_id)
        cart.refresh_from_db()
        return _set_cart_cookie(Response(_cart_payload(cart)), request, cart)
