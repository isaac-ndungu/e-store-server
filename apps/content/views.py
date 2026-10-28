from rest_framework import status
"""API views for the content app.

Storefront paths deliver published content pages and active banners to
unauthenticated visitors  -  content browsing is public by design. Admin
paths manage CRUD for both models and are gated behind staff access.

All mutations go through the content services layer; no view writes a model
field directly. Free-form text is sanitised at the service boundary, not in
the view or the serializer.
"""

from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import permissions

from rest_framework import serializers, status

from apps.core.permissions import IsStaffUser
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.content import cache as content_cache
from apps.content.models import BlogCategory, BlogTag
from apps.content.selectors import (
    get_banner_for_staff,
    get_blog_post_for_staff,
    get_page_for_staff,
    get_published_page_by_slug,
    get_published_post_by_slug,
    list_active_banners_for_placement,
    list_active_blog_categories,
    list_all_banners,
    list_all_blog_posts,
    list_all_pages,
    list_blog_tags,
    list_published_posts,
)
from apps.content.serializers import (
    BannerAdminSerializer,
    BannerCreateSerializer,
    BannerStorefrontSerializer,
    BannerUpdateSerializer,
    BlogCategoryCreateSerializer,
    BlogCategoryPublicSerializer,
    BlogCategoryUpdateSerializer,
    BlogPostAdminSerializer,
    BlogPostCreateSerializer,
    BlogPostDetailSerializer,
    BlogPostListSerializer,
    BlogPostUpdateSerializer,
    BlogTagCreateSerializer,
    BlogTagPublicSerializer,
    ContentPageAdminSerializer,
    ContentPageCreateSerializer,
    ContentPageStorefrontSerializer,
    ContentPageUpdateSerializer,
)
from apps.content.services import (
    create_banner,
    create_blog_category,
    create_blog_post,
    create_blog_tag,
    create_page,
    delete_banner,
    delete_blog_category,
    delete_blog_post,
    delete_blog_tag,
    delete_page,
    update_banner,
    update_blog_category,
    update_blog_post,
    update_page,
)
from apps.core.api import service_error_to_400 as _service_error_to_400


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

    Public by design  -  the storefront renders content pages outside the login
    wall. Unpublished or missing pages return 404, preventing enumeration of
    draft content.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="content_page_storefront",
        responses={200: ContentPageStorefrontSerializer},
    )
    def get(self, request, slug):
        """Return the published page's storefront shape.

        Args:
            request: the GET request.
            slug (str): the page slug.

        Returns:
            Response: the page content, or 404 when missing or unpublished.
        """
        generation = content_cache.get_page_generation()
        data = content_cache.get_cached_page(slug, generation)
        if data is None:
            page = _page_or_404(slug)
            data = ContentPageStorefrontSerializer(page).data
            content_cache.cache_page(slug, generation, data)
        return Response(data)


class BannerStorefrontView(APIView):
    """List active banners for a given placement.

    Public by design  -  the storefront fetches banners to populate layout
    regions. The ``placement`` query parameter is required; omitting it
    returns 400 to prevent unbounded queries across all placements.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="banner_storefront_list",
        responses={200: BannerStorefrontSerializer(many=True)},
    )
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
        generation = content_cache.get_banner_generation()
        rows = content_cache.get_cached_banner_rows(placement, generation)
        if rows is None:
            banners = list_active_banners_for_placement(placement)
            rows = BannerStorefrontSerializer(banners, many=True).data
            content_cache.cache_banner_rows(placement, generation, rows)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request)
        return paginator.get_paginated_response(page if page is not None else [])


class ContentPageAdminListView(APIView):
    """List all content pages for staff management."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_content_page_list",
        responses={200: ContentPageAdminSerializer(many=True)},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_content_page_create",
        request=ContentPageCreateSerializer,
        responses={201: ContentPageAdminSerializer},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_content_page_detail",
        responses={200: ContentPageAdminSerializer},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_content_page_update",
        request=ContentPageUpdateSerializer,
        responses={200: ContentPageAdminSerializer},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_content_page_delete",
        responses={204: None},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_banner_list",
        responses={200: BannerAdminSerializer(many=True)},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_banner_create",
        request=BannerCreateSerializer,
        responses={201: BannerAdminSerializer},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_banner_detail",
        responses={200: BannerAdminSerializer},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_banner_update",
        request=BannerUpdateSerializer,
        responses={200: BannerAdminSerializer},
    )
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

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_banner_delete",
        responses={204: None},
    )
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


def _parse_positive_int(raw):
    """Parse an optional positive-int query parameter, ignoring garbage.

    Args:
        raw (str | None): the raw query value.

    Returns:
        int | None: the parsed id, or None when missing or invalid.
    """
    if not raw:
        return None
    try:
        value = int(raw)
    except TypeError, ValueError:
        return None
    return value if value > 0 else None


def _parse_flag(raw):
    """Parse an optional boolean query flag as a strict opt-in.

    Only an explicit true-ish value enables the flag; anything else,
    including garbage, leaves it off.

    Args:
        raw (str | None): the raw query value.

    Returns:
        bool: True for true/1/yes, False otherwise.
    """
    if not raw:
        return False
    return raw.strip().lower() in ("true", "1", "yes")


class _BlogPostListPagination(PageNumberPagination):
    """Paginator for the public blog list with a capped page size.

    The cap lets small embeds request just a few rows without pulling a
    full page of results on every render.
    """

    page_size_query_param = "page_size"
    max_page_size = 50


def _staff_blog_post_or_404(post_id):
    """Return a blog post for staff management or raise HTTP 404.

    Args:
        post_id (int): the post primary key.

    Returns:
        BlogPost: the matched post.

    Raises:
        Http404: when no post matches the id.
    """
    post = get_blog_post_for_staff(post_id)
    if post is None:
        raise Http404
    return post


def _resolve_blog_category(category_id):
    """Resolve an optional category id to an instance.

    Args:
        category_id (int | None): the category primary key.

    Returns:
        BlogCategory | None: the category, or None when not supplied.

    Raises:
        serializers.ValidationError: when the id matches no category.
    """
    if category_id is None:
        return None
    try:
        return BlogCategory.objects.get(pk=category_id)
    except BlogCategory.DoesNotExist as exc:
        raise serializers.ValidationError(
            {"category_id": "No category matches this id."}
        ) from exc


class BlogPostListView(APIView):
    """List published blog posts for the storefront.

    Public by design  -  articles are crawlable content. Supports filtering
    by ``category``, ``tag``, ``search``, and ``product`` query parameters,
    and ``exclude_noindex`` for crawler feeds that must not advertise
    noindex URLs. ``page_size`` is honoured up to a cap so small embeds
    (e.g. related-guide strips) can ask for just a few rows.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="blog_post_list",
        responses={200: BlogPostListSerializer(many=True)},
    )
    def get(self, request):
        """Return paginated published posts, newest first.

        Args:
            request: the GET request with optional filters.

        Returns:
            Response: the paginated post list.
        """
        posts = list_published_posts(
            category_slug=request.query_params.get("category"),
            tag_slug=request.query_params.get("tag"),
            search=request.query_params.get("search"),
            product_id=_parse_positive_int(request.query_params.get("product")),
            exclude_noindex=_parse_flag(request.query_params.get("exclude_noindex")),
        )
        paginator = _BlogPostListPagination()
        page = paginator.paginate_queryset(posts, request)
        serializer = BlogPostListSerializer(
            page, many=True, context={"request": request}
        )
        return paginator.get_paginated_response(serializer.data)


class BlogPostDetailView(APIView):
    """Retrieve a published blog post by slug with SEO payloads.

    Public by design. Drafts and scheduled posts return 404.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="blog_post_detail",
        responses={200: BlogPostDetailSerializer},
    )
    def get(self, request, slug):
        """Return the post detail including meta and structured data.

        Args:
            request: the GET request.
            slug (str): the post slug.

        Returns:
            Response: the post detail, or 404 when missing or unpublished.
        """
        generation = content_cache.get_blog_generation()
        data = content_cache.get_cached_blog_post(slug, generation)
        if data is None:
            post = get_published_post_by_slug(slug)
            if post is None:
                raise Http404
            data = BlogPostDetailSerializer(post, context={"request": request}).data
            content_cache.cache_blog_post(slug, generation, data)
        return Response(data)


class BlogCategoryListView(APIView):
    """List active blog categories for archive pages and navigation."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="blog_category_list",
        responses={200: BlogCategoryPublicSerializer(many=True)},
    )
    def get(self, request):
        """Return active categories with published post counts.

        Args:
            request: the GET request.

        Returns:
            Response: the category list.
        """
        categories = list_active_blog_categories()
        serializer = BlogCategoryPublicSerializer(categories, many=True)
        return Response(serializer.data)


class BlogTagListView(APIView):
    """List blog tags for archive pages and navigation."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="blog_tag_list",
        responses={200: BlogTagPublicSerializer(many=True)},
    )
    def get(self, request):
        """Return tags ordered by name.

        Args:
            request: the GET request.

        Returns:
            Response: the tag list.
        """
        tags = list_blog_tags()
        serializer = BlogTagPublicSerializer(tags, many=True)
        return Response(serializer.data)


class BlogPostAdminListView(APIView):
    """List all blog posts for staff management."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_post_list",
        responses={200: BlogPostAdminSerializer(many=True)},
    )
    def get(self, request):
        """Return posts filtered by the optional ``status`` query flag.

        Args:
            request: the GET request (``?status=draft|published``).

        Returns:
            Response: the paginated post list for management.
        """
        posts = list_all_blog_posts(status=request.query_params.get("status"))
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(posts, request)
        serializer = BlogPostAdminSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class BlogPostAdminCreateView(APIView):
    """Create a blog post as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_post_create",
        request=BlogPostCreateSerializer,
        responses={201: BlogPostAdminSerializer},
    )
    def post(self, request):
        """Create a new blog post from the supplied payload.

        Args:
            request: the POST request carrying the post data.

        Returns:
            Response: ``201 Created`` with the admin post shape.
        """
        input_serializer = BlogPostCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        category = _resolve_blog_category(data.get("category_id"))
        post = _service_error_to_400(create_blog_post)(
            title=data["title"],
            slug=data["slug"],
            body=data["body"],
            excerpt=data.get("excerpt", ""),
            category=category,
            author_name=data.get("author_name", ""),
            status=data.get("status", "draft"),
            published_at=data.get("published_at"),
            meta_title=data.get("meta_title", ""),
            meta_description=data.get("meta_description", ""),
            canonical_url=data.get("canonical_url", ""),
            noindex=data.get("noindex", False),
            cover_image=data.get("cover_image"),
            og_image=data.get("og_image"),
            tag_ids=data.get("tag_ids", []),
            related_product_ids=data.get("related_product_ids", []),
        )
        serializer = BlogPostAdminSerializer(post)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BlogPostAdminDetailView(APIView):
    """Retrieve a blog post for staff management by id."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_post_detail",
        responses={200: BlogPostAdminSerializer},
    )
    def get(self, request, post_id):
        """Return the admin shape of the post.

        Args:
            request: the GET request.
            post_id (int): the post primary key.

        Returns:
            Response: the post data, or 404.
        """
        post = _staff_blog_post_or_404(post_id)
        serializer = BlogPostAdminSerializer(post)
        return Response(serializer.data)


class BlogPostAdminUpdateView(APIView):
    """Update a blog post as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_post_update",
        request=BlogPostUpdateSerializer,
        responses={200: BlogPostAdminSerializer},
    )
    def patch(self, request, post_id):
        """Partially update the post with the supplied fields.

        Args:
            request: the PATCH request carrying the update payload.
            post_id (int): the post primary key.

        Returns:
            Response: ``200 OK`` with the updated admin shape.
        """
        post = _staff_blog_post_or_404(post_id)
        input_serializer = BlogPostUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = dict(input_serializer.validated_data)
        if "category_id" in data:
            data["category"] = _resolve_blog_category(data.pop("category_id"))
        updated = _service_error_to_400(update_blog_post)(post=post, **data)
        serializer = BlogPostAdminSerializer(updated)
        return Response(serializer.data)


class BlogPostAdminDeleteView(APIView):
    """Delete a blog post as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_post_delete",
        responses={204: None},
    )
    def delete(self, request, post_id):
        """Delete the post.

        Args:
            request: the DELETE request.
            post_id (int): the post primary key.

        Returns:
            Response: ``204 No Content`` on success, or ``404``.
        """
        post = _staff_blog_post_or_404(post_id)
        delete_blog_post(post=post)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BlogCategoryAdminListView(APIView):
    """List blog categories for staff management."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_category_list",
        responses={200: BlogCategoryPublicSerializer(many=True)},
    )
    def get(self, request):
        """Return all categories ordered for management.

        Args:
            request: the GET request.

        Returns:
            Response: the category list.
        """
        categories = BlogCategory.objects.order_by("sort_order", "name", "pk")
        serializer = BlogCategoryPublicSerializer(categories, many=True)
        return Response(serializer.data)


class BlogCategoryAdminCreateView(APIView):
    """Create a blog category as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_category_create",
        request=BlogCategoryCreateSerializer,
        responses={201: BlogCategoryPublicSerializer},
    )
    def post(self, request):
        """Create a new blog category from the supplied payload.

        Args:
            request: the POST request carrying the category data.

        Returns:
            Response: ``201 Created`` with the category shape.
        """
        input_serializer = BlogCategoryCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        category = _service_error_to_400(create_blog_category)(
            **input_serializer.validated_data
        )
        serializer = BlogCategoryPublicSerializer(category)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BlogCategoryAdminUpdateView(APIView):
    """Update a blog category as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_category_update",
        request=BlogCategoryUpdateSerializer,
        responses={200: BlogCategoryPublicSerializer},
    )
    def patch(self, request, category_id):
        """Partially update the category with the supplied fields.

        Args:
            request: the PATCH request carrying the update payload.
            category_id (int): the category primary key.

        Returns:
            Response: ``200 OK`` with the updated shape, or ``404``.
        """
        try:
            category = BlogCategory.objects.get(pk=category_id)
        except BlogCategory.DoesNotExist:
            raise Http404 from None
        input_serializer = BlogCategoryUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(update_blog_category)(
            category=category, **input_serializer.validated_data
        )
        serializer = BlogCategoryPublicSerializer(updated)
        return Response(serializer.data)


class BlogCategoryAdminDeleteView(APIView):
    """Delete a blog category as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_category_delete",
        responses={204: None},
    )
    def delete(self, request, category_id):
        """Delete the category, keeping its posts with a null category.

        Args:
            request: the DELETE request.
            category_id (int): the category primary key.

        Returns:
            Response: ``204 No Content`` on success, or ``404``.
        """
        try:
            category = BlogCategory.objects.get(pk=category_id)
        except BlogCategory.DoesNotExist:
            raise Http404 from None
        delete_blog_category(category=category)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BlogTagAdminCreateView(APIView):
    """Create a blog tag as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_tag_create",
        request=BlogTagCreateSerializer,
        responses={201: BlogTagPublicSerializer},
    )
    def post(self, request):
        """Create a new blog tag from the supplied payload.

        Args:
            request: the POST request carrying the tag data.

        Returns:
            Response: ``201 Created`` with the tag shape.
        """
        input_serializer = BlogTagCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        tag = _service_error_to_400(create_blog_tag)(**input_serializer.validated_data)
        serializer = BlogTagPublicSerializer(tag)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BlogTagAdminDeleteView(APIView):
    """Delete a blog tag as a manager."""

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    @extend_schema(
        operation_id="admin_blog_tag_delete",
        responses={204: None},
    )
    def delete(self, request, tag_id):
        """Delete the tag.

        Args:
            request: the DELETE request.
            tag_id (int): the tag primary key.

        Returns:
            Response: ``204 No Content`` on success, or ``404``.
        """
        try:
            tag = BlogTag.objects.get(pk=tag_id)
        except BlogTag.DoesNotExist:
            raise Http404 from None
        delete_blog_tag(tag=tag)
        return Response(status=status.HTTP_204_NO_CONTENT)
