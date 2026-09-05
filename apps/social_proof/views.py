"""API views for the social proof app.

Public storefront endpoints: recording a product view, reading the live-viewer
count for one product, reading counts for many products at once, and the
recent-sales feed. All four are ``AllowAny`` deliberately — viewing a product,
or asking how many people are viewing it, is never gated — and they declare no
authentication classes so the common logged-in storefront path never trips the
session CSRF check for an action that mutates nothing sensitive.

A viewer's identity comes exclusively from the first-party visitor cookie the
server mints on first contact: a random hex value holding no personal data.
Client-supplied identifiers are ignored, so a caller cannot impersonate another
visitor or fabricate live-viewer counts by sending fake session keys. All of
the routes are throttled; the recording route has its own stricter scope
because every call can append a durable event row.
"""

import re

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.social_proof.constants import (
    BOT_USER_AGENT_PATTERN,
    VISITOR_COOKIE,
    VISITOR_COOKIE_MAX_AGE_DAYS,
    VISITOR_KEY_MAX_LENGTH,
)
from apps.social_proof.selectors import (
    get_active_product_by_slug,
    list_products_by_slugs,
    list_recent_sales,
)
from apps.social_proof.serializers import (
    BatchViewersQuerySerializer,
    LiveViewerCountSerializer,
    RecentSaleSerializer,
    RecentSalesQuerySerializer,
)
from apps.social_proof.services import (
    get_live_viewer_count,
    get_live_viewer_counts,
    is_feature_enabled,
    new_session_key,
    record_product_view,
)

_BOT_USER_AGENT_RE = re.compile(BOT_USER_AGENT_PATTERN, re.IGNORECASE)


def _visitor_key(request):
    """Resolve the caller's browsing identity from its visitor cookie.

    Returns a fresh, untrusted-free key when the cookie is absent or malformed
    so the caller can be handed one on the way out.

    Args:
        request: the incoming request.

    Returns:
        tuple: ``(session_key, needs_cookie)`` where ``needs_cookie`` is True
            when a fresh identifier was minted and must be persisted.
    """
    cookie_key = request.COOKIES.get(VISITOR_COOKIE, "")
    if cookie_key and len(cookie_key) <= VISITOR_KEY_MAX_LENGTH:
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
        VISITOR_COOKIE,
        session_key,
        max_age=VISITOR_COOKIE_MAX_AGE_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="Lax",
        secure=not settings.DEBUG,
    )
    return response


def _is_bot_request(request):
    """Return whether the request's user agent looks automated.

    Bot and crawler traffic is acknowledged but never recorded, so automated
    visits neither inflate live-viewer counts nor bloat the event table.

    Args:
        request: the incoming request.

    Returns:
        bool: True when the user agent matches a known bot pattern.
    """
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return bool(user_agent) and bool(_BOT_USER_AGENT_RE.search(user_agent))


class ProductViewRecordView(APIView):
    """Record a product view for the current visitor (public)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "social_proof_view"

    def post(self, request, slug=None):
        """Record the view and return the refreshed live-viewer count.

        Identity comes from the visitor cookie only; the request body is
        ignored entirely. Bot traffic gets the current count without being
        recorded.

        Args:
            request: the POST request.
            slug (str): the product slug from the URL.

        Returns:
            Response: ``201 Created`` with the live-viewer count (or ``200``
                for acknowledged bot traffic), or ``404`` when the product
                does not exist or is hidden.
        """
        product = get_active_product_by_slug(slug)
        if product is None:
            return Response(
                {"detail": "No such product."}, status=status.HTTP_404_NOT_FOUND
            )
        if _is_bot_request(request):
            return Response(
                LiveViewerCountSerializer(
                    {"live_viewers": get_live_viewer_count(product)}
                ).data
            )
        session_key, needs_cookie = _visitor_key(request)
        data = LiveViewerCountSerializer(
            {"live_viewers": record_product_view(product, session_key=session_key)}
        ).data
        response = Response(data, status=status.HTTP_201_CREATED)
        if needs_cookie:
            return _with_visitor_cookie(response, session_key)
        return response


class ProductViewersView(APIView):
    """Return the current live-viewer count for a product (public)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
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
        return Response(
            LiveViewerCountSerializer(
                {"live_viewers": get_live_viewer_count(product)}
            ).data
        )


class ViewerCountsBatchView(APIView):
    """Return live-viewer counts for several products at once (public).

    Served on a ``social-proof/`` path (not ``products/<slug>/viewers/``) so
    the catalog's ``products/<slug>`` pattern cannot capture the route.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request):
        """Return a slug-to-count mapping for the requested products.

        Slugs of missing or hidden products are filtered out, so their counts
        are simply absent from the response.

        Args:
            request: the GET request with the ``products`` query.

        Returns:
            Response: ``200 OK`` with ``{slug: count}`` for each visible
                product, or ``400`` when the query is invalid.
        """
        query = BatchViewersQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        products = list_products_by_slugs(query.validated_data["products"])
        counts = get_live_viewer_counts(products)
        payload = {product.slug: counts.get(product.pk, 0) for product in products}
        return Response({"live_viewers": payload})


class RecentSalesView(APIView):
    """Return recently completed purchases for the social-proof feed (public).

    The feed powers the storefront's "someone just bought X" popups. It
    exposes only the product, quantity, and purchase time — no customer data.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request):
        """Return the recent-sales feed, newest first.

        Args:
            request: the GET request with an optional ``limit``.

        Returns:
            Response: ``200 OK`` with the feed, or ``400`` when the query is
                invalid. An empty list when social proof is disabled.
        """
        query = RecentSalesQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        if not is_feature_enabled():
            return Response({"recent_sales": []})
        sales = list_recent_sales(query.validated_data["limit"])
        return Response({"recent_sales": RecentSaleSerializer(sales, many=True).data})
