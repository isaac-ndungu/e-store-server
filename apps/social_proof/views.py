"""API views for the social proof app.

Two public endpoints: recording a product view and reading the current
live-viewer count. Both are ``AllowAny`` deliberately — viewing a product (or
asking how many people are viewing it) is never gated, and both are throttled
with the shared public scope. Views stay thin: resolve the product through the
selector, call the service, return the count.

A viewer's identity comes from an explicit ``session_key`` in the payload,
the caller's established Django session, or a first-party visitor cookie the
endpoint issues on first contact, so repeated views by the same shopper are
counted once while distinct shoppers each count toward the live total.
"""

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.social_proof.cache import LIVE_VIEWER_WINDOW_SECONDS
from apps.social_proof.selectors import get_active_product_by_slug
from apps.social_proof.serializers import ProductViewSerializer
from apps.social_proof.services import (
    get_live_viewer_count,
    new_session_key,
    record_product_view,
)

# First-party cookie carrying the anonymous visitor identity the live-viewer
# counters key on. It holds no token and no personal data — just a random hex
# identifier that distinguishes one browser from another.
_VISITOR_COOKIE = "e_store_visitor"


def _visitor_session_key(request, provided_key):
    """Resolve the browsing session identifier for a request.

    Args:
        request: the incoming request.
        provided_key (str): an explicit ``session_key`` from the payload, or
            empty.

    Returns:
        tuple: ``(session_key or None, needs_cookie)`` where ``needs_cookie``
            is True when a fresh identifier was generated and must be handed
            back to the caller so the next request reuses it.
    """
    if provided_key:
        return provided_key, False
    request_session_key = getattr(request.session, "session_key", None)
    if request_session_key:
        return request_session_key, False
    cookie_key = request.COOKIES.get(_VISITOR_COOKIE)
    if cookie_key:
        return cookie_key, False
    return new_session_key(), True


def _with_visitor_cookie(response, session_key):
    """Set the anonymous visitor cookie on a response when issued fresh.

    Args:
        response (Response): the DRF response.
        session_key (str): the identifier to persist.

    Returns:
        Response: the response with the cookie attached.
    """
    response.set_cookie(
        _VISITOR_COOKIE,
        session_key,
        max_age=LIVE_VIEWER_WINDOW_SECONDS,
        httponly=False,
        samesite="Lax",
        secure=not settings.DEBUG,
    )
    return response


class ProductViewRecordView(APIView):
    """Record a product view for the current browsing session (public)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def post(self, request, slug=None):
        """Record the view and return the refreshed live-viewer count.

        Args:
            request: the POST request; the payload may carry an optional
                ``session_key`` identifying the browsing session.
            slug (str): the product slug from the URL.

        Returns:
            Response: ``201 Created`` with the live-viewer count, or ``404``
                when the product does not exist or is hidden.
        """
        product = get_active_product_by_slug(slug)
        if product is None:
            return Response(
                {"detail": "No such product."}, status=status.HTTP_404_NOT_FOUND
            )
        input_serializer = ProductViewSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        provided_key = input_serializer.validated_data.get("session_key", "").strip()
        session_key, needs_cookie = _visitor_session_key(request, provided_key)

        response = Response(
            {"live_viewers": record_product_view(product, session_key=session_key)},
            status=status.HTTP_201_CREATED,
        )
        if needs_cookie:
            return _with_visitor_cookie(response, session_key)
        return response


class ProductViewersView(APIView):
    """Return the current live-viewer count for a product (public)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request, slug=None):
        """Return the live-viewer count.

        Args:
            request: the GET request.
            slug (str): the product slug from the URL.

        Returns:
            Response: ``200 OK`` with the live-viewer count, or ``404`` when
                the product does not exist or is hidden.
        """
        product = get_active_product_by_slug(slug)
        if product is None:
            return Response(
                {"detail": "No such product."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response({"live_viewers": get_live_viewer_count(product)})
