"""API views for the content app.

Storefront paths deliver published content pages and active banners to
unauthenticated visitors — content browsing is public by design. Admin
paths manage CRUD for both models and are gated behind the manager role.

All mutations go through the content services layer; no view writes a model
field directly. Free-form text is sanitised at the service boundary, not in
the view or the serializer.
"""

from django.http import Http404
from rest_framework import permissions, serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManager
from apps.content.selectors import (
    get_banner_for_staff,
    get_page_for_staff,
    get_published_page_by_slug,
    list_active_banners_for_placement,
    list_all_banners,
    list_all_pages,
)
from apps.content.serializers import (
    BannerAdminSerializer,
    BannerCreateSerializer,
    BannerStorefrontSerializer,
    BannerUpdateSerializer,
    ContentPageAdminSerializer,
    ContentPageCreateSerializer,
    ContentPageStorefrontSerializer,
    ContentPageUpdateSerializer,
)
from apps.content.services import (
    create_banner,
    create_page,
    delete_banner,
    delete_page,
    update_banner,
    update_page,
)
from apps.orders.views import _service_error_to_400


def _page_or_404(slug):
    """Resolve a published content page by slug or raise HTTP 404.

    Args:
        slug (str): the page slug.

    Returns:
        ContentPage: the resolved page.

    Raises:
        Http404: when the page is missing or unpublished.
    """
    page = get_published_page_by_slug(slug)
    if page is None:
        raise Http404
    return page


def _staff_page_or_404(page_id):
    """Return a content page for staff management or raise HTTP 404.

    Args:
        page_id (int): the page primary key.

    Returns:
        ContentPage: the matched page.

    Raises:
        Http404: when no page matches the id.
    """
    page = get_page_for_staff(page_id)
    if page is None:
        raise Http404
    return page


def _staff_banner_or_404(banner_id):
    """Return a banner for staff management or raise HTTP 404.

    Args:
        banner_id (int): the banner primary key.

    Returns:
        Banner: the matched banner.

    Raises:
        Http404: when no banner matches the id.
    """
    banner = get_banner_for_staff(banner_id)
    if banner is None:
        raise Http404
    return banner


class ContentPageStorefrontView(APIView):
    """Retrieve a published content page by slug.

    Public by design — the storefront renders content pages outside the login
    wall. Unpublished or missing pages return 404, preventing enumeration of
    draft content.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    def get(self, request, slug):
        """Return the published page's storefront shape.

        Args:
            request: the GET request.
            slug (str): the page slug.

        Returns:
            Response: the page content, or 404 when missing or unpublished.
        """
        page = _page_or_404(slug)
        serializer = ContentPageStorefrontSerializer(page)
        return Response(serializer.data)


class BannerStorefrontView(APIView):
    """List active banners for a given placement.

    Public by design — the storefront fetches banners to populate layout
    regions. The ``placement`` query parameter is required; omitting it
    returns 400 to prevent unbounded queries across all placements.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    def get(self, request):
        """Return active banners matching the requested placement.

        Args:
            request: the GET request with ``?placement=...``.

        Returns:
            Response: the paginated banner list, or 400 when placement is
                missing.
        """
        placement = request.query_params.get("placement")
        if not placement:
            raise serializers.ValidationError(
                {"placement": "This query parameter is required."}
            )
        banners = list_active_banners_for_placement(placement)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(banners, request)
        serializer = BannerStorefrontSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ContentPageAdminListView(APIView):
    """List all content pages for staff management."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request):
        """Return pages filtered by the optional ``published`` query flag.

        Args:
            request: the GET request (``?published=true|false``).

        Returns:
            Response: the paginated page list for management.
        """
        published = _parse_published_param(request)
        pages = list_all_pages(published=published)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(pages, request)
        serializer = ContentPageAdminSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ContentPageAdminCreateView(APIView):
    """Create a content page as a manager."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request):
        """Create a new content page from the supplied payload.

        Args:
            request: the POST request carrying the page data.

        Returns:
            Response: ``201 Created`` with the admin page shape, or ``400``
                for validation errors.
        """
        input_serializer = ContentPageCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        page = _service_error_to_400(create_page)(
            title=data["title"],
            slug=data["slug"],
            body=data["body"],
            is_published=data.get("is_published", True),
            meta_title=data.get("meta_title", ""),
            meta_description=data.get("meta_description", ""),
        )
        serializer = ContentPageAdminSerializer(page)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ContentPageAdminDetailView(APIView):
    """Retrieve a content page for staff management by id."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request, page_id):
        """Return the admin shape of the page.

        Args:
            request: the GET request.
            page_id (int): the page primary key.

        Returns:
            Response: the page data, or 404.
        """
        page = _staff_page_or_404(page_id)
        serializer = ContentPageAdminSerializer(page)
        return Response(serializer.data)


class ContentPageAdminUpdateView(APIView):
    """Update a content page as a manager."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def patch(self, request, page_id):
        """Partially update the page with the supplied fields.

        Args:
            request: the PATCH request carrying the update payload.
            page_id (int): the page primary key.

        Returns:
            Response: ``200 OK`` with the updated admin shape, ``400`` for
                validation errors, or ``404`` when the page is missing.
        """
        page = _staff_page_or_404(page_id)
        input_serializer = ContentPageUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(update_page)(
            page=page, **input_serializer.validated_data
        )
        serializer = ContentPageAdminSerializer(updated)
        return Response(serializer.data)


class ContentPageAdminDeleteView(APIView):
    """Delete a content page as a manager."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def delete(self, request, page_id):
        """Delete the page.

        Args:
            request: the DELETE request.
            page_id (int): the page primary key.

        Returns:
            Response: ``204 No Content`` on success, or ``404``.
        """
        page = _staff_page_or_404(page_id)
        delete_page(page=page)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BannerAdminListView(APIView):
    """List all banners for staff management."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request):
        """Return banners filtered by optional ``active`` and ``placement`` flags.

        Args:
            request: the GET request with optional query params.

        Returns:
            Response: the paginated banner list for management.
        """
        active = _parse_active_param(request)
        placement = request.query_params.get("placement")
        banners = list_all_banners(active=active, placement=placement)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(banners, request)
        serializer = BannerAdminSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class BannerAdminCreateView(APIView):
    """Create a banner as a manager."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request):
        """Create a new banner from the supplied payload.

        The image is validated by the serializer (Django's
        ``ImageField`` checks file type and content). The banner title is
        sanitised at the service boundary.

        Args:
            request: the POST request carrying the banner data.

        Returns:
            Response: ``201 Created`` with the admin banner shape, or ``400``
                for validation errors.
        """
        input_serializer = BannerCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        banner = _service_error_to_400(create_banner)(
            title=data.get("title", ""),
            image=data["image"],
            link_url=data.get("link_url", ""),
            placement=data["placement"],
            sort_order=data.get("sort_order", 0),
            starts_at=data.get("starts_at"),
            ends_at=data.get("ends_at"),
            is_active=data.get("is_active", True),
        )
        serializer = BannerAdminSerializer(banner)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BannerAdminDetailView(APIView):
    """Retrieve a banner for staff management by id."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request, banner_id):
        """Return the admin shape of the banner.

        Args:
            request: the GET request.
            banner_id (int): the banner primary key.

        Returns:
            Response: the banner data, or 404.
        """
        banner = _staff_banner_or_404(banner_id)
        serializer = BannerAdminSerializer(banner)
        return Response(serializer.data)


class BannerAdminUpdateView(APIView):
    """Update a banner as a manager."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def patch(self, request, banner_id):
        """Partially update the banner with the supplied fields.

        Args:
            request: the PATCH request carrying the update payload.
            banner_id (int): the banner primary key.

        Returns:
            Response: ``200 OK`` with the updated admin shape, ``400`` for
                validation errors, or ``404`` when the banner is missing.
        """
        banner = _staff_banner_or_404(banner_id)
        input_serializer = BannerUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(update_banner)(
            banner=banner, **input_serializer.validated_data
        )
        serializer = BannerAdminSerializer(updated)
        return Response(serializer.data)


class BannerAdminDeleteView(APIView):
    """Delete a banner as a manager."""

    permission_classes = [IsManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def delete(self, request, banner_id):
        """Delete the banner.

        Args:
            request: the DELETE request.
            banner_id (int): the banner primary key.

        Returns:
            Response: ``204 No Content`` on success, or ``404``.
        """
        banner = _staff_banner_or_404(banner_id)
        delete_banner(banner=banner)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _parse_published_param(request):
    """Parse the optional ``published`` moderation filter as a strict tri-state.

    Args:
        request: the HTTP request carrying the query string.

    Returns:
        bool | None: True/False to filter on that state, None for no filter.
    """
    raw = request.query_params.get("published")
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised in ("true", "1", "yes"):
        return True
    if normalised in ("false", "0", "no"):
        return False
    return None


def _parse_active_param(request):
    """Parse the optional ``active`` filter as a strict tri-state.

    Args:
        request: the HTTP request carrying the query string.

    Returns:
        bool | None: True/False to filter on that state, None for no filter.
    """
    raw = request.query_params.get("active")
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised in ("true", "1", "yes"):
        return True
    if normalised in ("false", "0", "no"):
        return False
    return None
