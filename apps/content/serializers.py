"""API serializers for the content app.

Read serializers expose only white-listed, storefront-safe fields: the public
page shape omits ``meta_title``/``meta_description`` (reserved for the detail
page or SEO layer) while the admin shape includes every editable field.

Write serializers whitelist exactly what a caller may supply. The body is
sanitised at the service boundary — the serializer enforces length and
presence constraints only.
"""

from rest_framework import serializers

from apps.content.constants import (
    BANNER_LINK_URL_MAX_LENGTH,
    BANNER_TITLE_MAX_LENGTH,
    PAGE_BODY_MAX_LENGTH,
    PAGE_TITLE_MAX_LENGTH,
)
from apps.content.models import Banner, ContentPage


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
    """Input for a staff member updating a content page.

    Every field is optional — only supplied fields are applied.
    """

    title = serializers.CharField(max_length=PAGE_TITLE_MAX_LENGTH, required=False)
    slug = serializers.SlugField(required=False)
    body = serializers.CharField(max_length=PAGE_BODY_MAX_LENGTH, required=False)
    is_published = serializers.BooleanField(required=False)
    meta_title = serializers.CharField(
        max_length=PAGE_TITLE_MAX_LENGTH, required=False, allow_blank=True
    )
    meta_description = serializers.CharField(required=False, allow_blank=True)


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
    image = serializers.ImageField()
    link_url = serializers.CharField(
        max_length=BANNER_LINK_URL_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    placement = serializers.CharField(max_length=50)
    sort_order = serializers.IntegerField(min_value=0, default=0)
    starts_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    ends_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    is_active = serializers.BooleanField(default=True)


class BannerUpdateSerializer(serializers.Serializer):
    """Input for a staff member updating a banner.

    Every field is optional — only supplied fields are applied.
    """

    title = serializers.CharField(
        max_length=BANNER_TITLE_MAX_LENGTH, required=False, allow_blank=True
    )
    image = serializers.ImageField(required=False)
    link_url = serializers.CharField(
        max_length=BANNER_LINK_URL_MAX_LENGTH, required=False, allow_blank=True
    )
    placement = serializers.CharField(max_length=50, required=False)
    sort_order = serializers.IntegerField(min_value=0, required=False)
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)
