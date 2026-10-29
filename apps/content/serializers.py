"""API serializers for the content app.

Read serializers expose only white-listed, storefront-safe fields: the public
page shape omits ``meta_title``/``meta_description`` (reserved for the detail
page or SEO layer) while the admin shape includes every editable field.

Write serializers whitelist exactly what a caller may supply. The body is
sanitised at the service boundary  -  the serializer enforces length and
presence constraints only.
"""

import bleach
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework import serializers

from apps.catalog.validators import validate_image_upload
from apps.content.constants import (
    BANNER_LINK_URL_MAX_LENGTH,
    BANNER_TITLE_MAX_LENGTH,
    BLOG_AUTHOR_MAX_LENGTH,
    BLOG_BODY_MAX_LENGTH,
    BLOG_CANONICAL_URL_MAX_LENGTH,
    BLOG_CATEGORY_NAME_MAX_LENGTH,
    BLOG_EXCERPT_MAX_LENGTH,
    BLOG_META_TITLE_MAX_LENGTH,
    BLOG_TAG_NAME_MAX_LENGTH,
    BLOG_TITLE_MAX_LENGTH,
    PAGE_BODY_MAX_LENGTH,
    PAGE_TITLE_MAX_LENGTH,
)
from apps.content.models import Banner, BlogCategory, BlogPost, BlogTag, ContentPage


class ContentPageStorefrontSerializer(serializers.ModelSerializer):
    """Public storefront shape for a published content page."""

    class Meta:
        model = ContentPage
        fields = ["id", "title", "slug", "body", "updated_at"]
        read_only_fields = fields


class ContentPageAdminSerializer(serializers.ModelSerializer):
    """Staff management shape with all editable and metadata fields."""

    class Meta:
        model = ContentPage
        fields = [
            "id",
            "title",
            "slug",
            "body",
            "is_published",
            "meta_title",
            "meta_description",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]


class ContentPageCreateSerializer(serializers.Serializer):
    """Input for a staff member creating a content page."""

    title = serializers.CharField(max_length=PAGE_TITLE_MAX_LENGTH)
    slug = serializers.SlugField()
    body = serializers.CharField(max_length=PAGE_BODY_MAX_LENGTH)
    is_published = serializers.BooleanField(default=True)
    meta_title = serializers.CharField(
        max_length=PAGE_TITLE_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    meta_description = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class ContentPageUpdateSerializer(serializers.Serializer):
    """Input for updating a legal/static page.

    Every field is optional  -  only supplied fields are applied.
    """

    title = serializers.CharField(max_length=PAGE_TITLE_MAX_LENGTH, required=False)
    slug = serializers.SlugField(required=False)
    body = serializers.CharField(max_length=PAGE_BODY_MAX_LENGTH, required=False)
    is_published = serializers.BooleanField(required=False)
    meta_title = serializers.CharField(
        max_length=PAGE_TITLE_MAX_LENGTH, required=False, allow_blank=True
    )
    meta_description = serializers.CharField(required=False, allow_blank=True)

    def validate_title(self, value):
        """Sanitize title."""
        if value is None or value == "":
            return value
        return bleach.clean(value, tags=set(), strip=True).strip()

    def validate_meta_title(self, value):
        """Sanitize meta title."""
        if value is None or value == "":
            return value
        return bleach.clean(value, tags=set(), strip=True).strip()

    def validate_meta_description(self, value):
        """Sanitize meta description."""
        if value is None or value == "":
            return value
        return bleach.clean(value, tags=set(), strip=True).strip()


class BannerStorefrontSerializer(serializers.ModelSerializer):
    """Public storefront shape for an active banner."""

    class Meta:
        model = Banner
        fields = ["id", "title", "image", "link_url", "placement", "sort_order"]
        read_only_fields = fields


class BannerAdminSerializer(serializers.ModelSerializer):
    """Staff management shape with schedule and active fields."""

    class Meta:
        model = Banner
        fields = [
            "id",
            "title",
            "image",
            "link_url",
            "placement",
            "sort_order",
            "starts_at",
            "ends_at",
            "is_active",
        ]
        read_only_fields = ["id"]


class BannerCreateSerializer(serializers.Serializer):
    """Input for a staff member creating a banner."""

    title = serializers.CharField(
        max_length=BANNER_TITLE_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    link_url = serializers.CharField(
        max_length=BANNER_LINK_URL_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    image = serializers.ImageField(validators=[validate_image_upload])
    placement = serializers.CharField(max_length=50)
    sort_order = serializers.IntegerField(min_value=0, default=0)
    starts_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    ends_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    is_active = serializers.BooleanField(default=True)

    def validate_title(self, value):
        """Sanitize title."""
        if value is None or value == "":
            return value
        return bleach.clean(value, tags=set(), strip=True).strip()

    def validate_link_url(self, value):
        """Return the link URL after rejecting unsafe schemes.

        Args:
            value (str): the submitted link URL.

        Returns:
            str: the validated link URL.

        Raises:
            serializers.ValidationError: if the scheme is not http(s) or
                relative.
        """
        if not value or value.startswith("/"):
            if value.startswith("//"):
                raise serializers.ValidationError(
                    "link_url must be an http(s) URL or a relative path "
                    "starting with '/'."
                )
            return value
        if not url_has_allowed_host_and_scheme(
            value, allowed_hosts=None, require_https=False
        ):
            raise serializers.ValidationError(
                "link_url must be an http(s) URL or a relative path starting "
                "with '/'."
            )
        return value


class BannerUpdateSerializer(serializers.Serializer):
    """Input for a staff member updating a banner.

    Every field is optional  -  only supplied fields are applied.
    """

    title = serializers.CharField(
        max_length=BANNER_TITLE_MAX_LENGTH, required=False, allow_blank=True
    )
    link_url = serializers.CharField(
        max_length=BANNER_LINK_URL_MAX_LENGTH, required=False, allow_blank=True
    )
    image = serializers.ImageField(required=False, validators=[validate_image_upload])
    placement = serializers.CharField(max_length=50, required=False)
    sort_order = serializers.IntegerField(min_value=0, required=False)
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)

    def validate_link_url(self, value):
        """Return the link URL after rejecting unsafe schemes.

        Args:
            value (str): the submitted link URL (may be blank or omitted).

        Returns:
            str: the validated link URL.

        Raises:
            serializers.ValidationError: if the scheme is not http(s) or
                relative.
        """
        if not value or value.startswith("/"):
            if value.startswith("//"):
                raise serializers.ValidationError(
                    "link_url must be an http(s) URL or a relative path "
                    "starting with '/'."
                )
            return value
        if not url_has_allowed_host_and_scheme(
            value, allowed_hosts=None, require_https=False
        ):
            raise serializers.ValidationError(
                "link_url must be an http(s) URL or a relative path starting "
                "with '/'."
            )
        return value


class BlogCategoryPublicSerializer(serializers.ModelSerializer):
    """Public archive shape for a blog category with its post count."""

    post_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = BlogCategory
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "meta_title",
            "meta_description",
            "post_count",
        ]
        read_only_fields = fields


class BlogTagPublicSerializer(serializers.ModelSerializer):
    """Public shape for a blog tag."""

    class Meta:
        model = BlogTag
        fields = ["id", "name", "slug"]
        read_only_fields = fields


class BlogRelatedProductSerializer(serializers.Serializer):
    """Thin product card embedded in a blog post for internal linking."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    slug = serializers.SlugField()
    price = serializers.DecimalField(max_digits=12, decimal_places=2)
    image = serializers.CharField(required=False, allow_blank=True, default="")


class BlogPostListSerializer(serializers.ModelSerializer):
    """Thin storefront shape for blog listing pages (no body)."""

    category = BlogCategoryPublicSerializer(read_only=True)
    tags = BlogTagPublicSerializer(many=True, read_only=True)

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "cover_image",
            "category",
            "tags",
            "author_name",
            "published_at",
            "reading_minutes",
            "noindex",
            "updated_at",
        ]
        read_only_fields = fields


class BlogPostDetailSerializer(serializers.ModelSerializer):
    """Full storefront shape for a single post with SEO and link payloads."""

    category = BlogCategoryPublicSerializer(read_only=True)
    tags = BlogTagPublicSerializer(many=True, read_only=True)
    related_products = serializers.SerializerMethodField()
    related_posts = serializers.SerializerMethodField()
    json_ld = serializers.SerializerMethodField()

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "body",
            "cover_image",
            "og_image",
            "category",
            "tags",
            "author_name",
            "published_at",
            "reading_minutes",
            "word_count",
            "meta_title",
            "meta_description",
            "canonical_url",
            "noindex",
            "related_products",
            "related_posts",
            "json_ld",
            "updated_at",
        ]
        read_only_fields = fields

    def get_related_products(self, obj):
        """Return linked products as thin cards for storefront linking.

        Args:
            obj (BlogPost): the post being serialized.

        Returns:
            list: product cards with id, name, slug, and price.
        """
        products = list(obj.related_products.all()[:8])
        cards = []
        for product in products:
            image = ""
            primary = product.images.filter(is_primary=True).first()
            if primary is None:
                primary = product.images.order_by("sort_order", "pk").first()
            if primary is not None:
                try:
                    image = primary.image.url
                except ValueError:
                    image = ""
            cards.append(
                {
                    "id": product.pk,
                    "name": product.name,
                    "slug": product.slug,
                    "price": str(product.price),
                    "image": image,
                }
            )
        return cards

    def get_related_posts(self, obj):
        """Return same-category published posts excluding this one.

        Args:
            obj (BlogPost): the post being serialized.

        Returns:
            list: slim related-post rows for internal linking.
        """
        from apps.content.selectors import list_related_posts

        related = list_related_posts(post=obj, limit=3)
        return [
            {
                "title": item.title,
                "slug": item.slug,
                "excerpt": item.excerpt,
                "published_at": item.published_at,
            }
            for item in related
        ]

    def get_json_ld(self, obj):
        """Return an Article structured-data block for the storefront.

        The storefront injects this verbatim as ``application/ld+json`` so
        crawlers get headline, dates, and author without extra computation.
        ``url`` and ``mainEntityOfPage`` are absolute storefront addresses
        built from the configured frontend base, matching the canonical
        the storefront emits for the same slug.

        Args:
            obj (BlogPost): the post being serialized.

        Returns:
            dict: a schema.org Article object.
        """
        from django.conf import settings as django_settings

        request = self.context.get("request")
        image_url = ""
        if obj.cover_image:
            try:
                image_url = obj.cover_image.url
                if request is not None and image_url.startswith("/"):
                    image_url = request.build_absolute_uri(image_url)
            except ValueError:
                image_url = ""
        base = django_settings.FRONTEND_URL.rstrip("/")
        url = f"{base}/blog/{obj.slug}/"
        return {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": obj.meta_title or obj.title,
            "description": obj.meta_description or obj.excerpt,
            "image": [image_url] if image_url else [],
            "url": url,
            "mainEntityOfPage": url,
            "datePublished": (
                obj.published_at.isoformat() if obj.published_at else None
            ),
            "dateModified": (obj.updated_at.isoformat() if obj.updated_at else None),
            "author": {"@type": "Person", "name": obj.author_name or "Staff"},
        }


class BlogPostAdminSerializer(serializers.ModelSerializer):
    """Staff management shape with every editable field."""

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "body",
            "cover_image",
            "og_image",
            "category",
            "tags",
            "related_products",
            "author_name",
            "status",
            "published_at",
            "meta_title",
            "meta_description",
            "canonical_url",
            "noindex",
            "reading_minutes",
            "word_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "reading_minutes", "word_count", "created_at"]


class BlogPostCreateSerializer(serializers.Serializer):
    """Input for a staff member creating a blog post."""

    title = serializers.CharField(max_length=BLOG_TITLE_MAX_LENGTH)
    slug = serializers.SlugField()
    body = serializers.CharField(max_length=BLOG_BODY_MAX_LENGTH)
    excerpt = serializers.CharField(
        max_length=BLOG_EXCERPT_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    category_id = serializers.IntegerField(
        required=False, allow_null=True, default=None
    )
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    related_product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    author_name = serializers.CharField(
        max_length=BLOG_AUTHOR_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    status = serializers.ChoiceField(
        choices=["draft", "published"],
        required=False,
        default="draft",
    )
    published_at = serializers.DateTimeField(
        required=False, allow_null=True, default=None
    )
    meta_title = serializers.CharField(
        max_length=BLOG_META_TITLE_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    meta_description = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )
    canonical_url = serializers.CharField(
        max_length=BLOG_CANONICAL_URL_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    noindex = serializers.BooleanField(required=False, default=False)
    cover_image = serializers.ImageField(
        required=False, validators=[validate_image_upload]
    )
    og_image = serializers.ImageField(
        required=False, validators=[validate_image_upload]
    )


class BlogPostUpdateSerializer(serializers.Serializer):
    """Input for a staff member updating a blog post.

    Every field is optional  -  only supplied fields are applied.
    """

    title = serializers.CharField(max_length=BLOG_TITLE_MAX_LENGTH, required=False)
    slug = serializers.SlugField(required=False)
    body = serializers.CharField(max_length=BLOG_BODY_MAX_LENGTH, required=False)
    excerpt = serializers.CharField(
        max_length=BLOG_EXCERPT_MAX_LENGTH,
        required=False,
        allow_blank=True,
    )
    category_id = serializers.IntegerField(required=False, allow_null=True)
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )
    related_product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )
    author_name = serializers.CharField(
        max_length=BLOG_AUTHOR_MAX_LENGTH,
        required=False,
        allow_blank=True,
    )
    status = serializers.ChoiceField(choices=["draft", "published"], required=False)
    published_at = serializers.DateTimeField(required=False, allow_null=True)
    meta_title = serializers.CharField(
        max_length=BLOG_META_TITLE_MAX_LENGTH,
        required=False,
        allow_blank=True,
    )
    meta_description = serializers.CharField(required=False, allow_blank=True)
    canonical_url = serializers.CharField(
        max_length=BLOG_CANONICAL_URL_MAX_LENGTH,
        required=False,
        allow_blank=True,
    )
    noindex = serializers.BooleanField(required=False)
    cover_image = serializers.ImageField(
        required=False, validators=[validate_image_upload]
    )
    og_image = serializers.ImageField(
        required=False, validators=[validate_image_upload]
    )


class BlogCategoryCreateSerializer(serializers.Serializer):
    """Input for a staff member creating a blog category."""

    name = serializers.CharField(max_length=BLOG_CATEGORY_NAME_MAX_LENGTH)
    slug = serializers.SlugField()
    description = serializers.CharField(required=False, allow_blank=True, default="")
    meta_title = serializers.CharField(
        max_length=BLOG_META_TITLE_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    meta_description = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    sort_order = serializers.IntegerField(min_value=0, required=False, default=0)
    is_active = serializers.BooleanField(required=False, default=True)


class BlogCategoryUpdateSerializer(serializers.Serializer):
    """Input for a staff member updating a blog category."""

    name = serializers.CharField(
        max_length=BLOG_CATEGORY_NAME_MAX_LENGTH, required=False
    )
    slug = serializers.SlugField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    meta_title = serializers.CharField(
        max_length=BLOG_META_TITLE_MAX_LENGTH,
        required=False,
        allow_blank=True,
    )
    meta_description = serializers.CharField(required=False, allow_blank=True)
    sort_order = serializers.IntegerField(min_value=0, required=False)
    is_active = serializers.BooleanField(required=False)


class BlogTagCreateSerializer(serializers.Serializer):
    """Input for a staff member creating a blog tag."""

    name = serializers.CharField(max_length=BLOG_TAG_NAME_MAX_LENGTH)
    slug = serializers.SlugField()
