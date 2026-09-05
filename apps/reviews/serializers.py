"""API serializers for the reviews app.

Read serializers expose only white-listed, storefront-safe fields: the
reviewer's display handle (username) is shown, never their email or phone
number, and moderation shapes keep author and product context for staff.

Write serializers whitelist exactly what a caller may supply. A review names a
rating, optional headline/body, optional photo URLs from the photo-upload
endpoint, and an optional order-line id for the verified-purchase badge; the
reviewer identity always comes from the request's authenticated user, never a
client-supplied field. The question and answer serializers accept a single
free-form text field. Photo references must look like real http(s) URLs so a
``javascript:`` or ``data:`` payload cannot be stored and later rendered.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

from apps.reviews.constants import (
    ANSWER_MAX_LENGTH,
    MAX_REVIEW_RATING,
    QUESTION_MAX_LENGTH,
    REVIEW_BODY_MAX_LENGTH,
    REVIEW_MAX_PHOTOS,
    REVIEW_TITLE_MAX_LENGTH,
)
from apps.reviews.models import ProductAnswer, ProductQuestion, Review


class ReviewAuthorSerializer(serializers.Serializer):
    """Public reviewer identity — the display handle only, never contact data."""

    username = serializers.CharField()


class AnswerSerializer(serializers.ModelSerializer):
    """Storefront-facing answer shape with the author's display handle."""

    author = serializers.SerializerMethodField()

    class Meta:
        model = ProductAnswer
        fields = ["id", "is_staff_answer", "answer", "author", "created_at"]
        read_only_fields = fields

    def get_author(self, obj):
        """Return the answering staff member's display handle, if any.

        Args:
            obj (ProductAnswer): the answer being serialized.

        Returns:
            dict | None: ``{"username": ...}`` or None when the authoring
                account was deleted.
        """
        if obj.answered_by is None:
            return None
        return {"username": obj.answered_by.username}


class ReviewSerializer(serializers.ModelSerializer):
    """Storefront-facing review shape."""

    user = ReviewAuthorSerializer(read_only=True)
    verified_purchase = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            "id",
            "rating",
            "title",
            "body",
            "photos",
            "verified_purchase",
            "user",
            "created_at",
        ]
        read_only_fields = fields

    def get_verified_purchase(self, obj):
        """Return whether the review is backed by a purchase order line.

        The badge simply reflects whether an ``OrderItem`` was claimed and
        validated at creation — the service guarantees that line belonged to
        the reviewer's own completed order.

        Args:
            obj (Review): the review being serialized.

        Returns:
            bool: True when the review carries a verified-purchase line.
        """
        return obj.order_item_id is not None


class QuestionSerializer(serializers.ModelSerializer):
    """Storefront-facing question shape with its answers oldest-first."""

    user = ReviewAuthorSerializer(read_only=True)
    answers = AnswerSerializer(many=True, read_only=True)

    class Meta:
        model = ProductQuestion
        fields = ["id", "question", "user", "answers", "created_at"]
        read_only_fields = fields


class ReviewCreateSerializer(serializers.Serializer):
    """Input for a customer creating a review.

    ``order_item_id`` optionally marks the review as a verified purchase; the
    service re-checks the line belongs to the caller's completed order.
    ``photos`` cites URLs produced by the photo-upload endpoint and capped at
    ``REVIEW_MAX_PHOTOS`` entries.
    """

    rating = serializers.IntegerField(min_value=1, max_value=MAX_REVIEW_RATING)
    title = serializers.CharField(
        max_length=REVIEW_TITLE_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    body = serializers.CharField(
        max_length=REVIEW_BODY_MAX_LENGTH,
        required=False,
        allow_blank=True,
        default="",
    )
    photos = serializers.ListField(
        child=serializers.CharField(max_length=500),
        required=False,
        allow_empty=True,
        max_length=REVIEW_MAX_PHOTOS,
        default=list,
    )
    order_item_id = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )

    def validate_photos(self, value):
        """Reject anything that is not a valid absolute http(s) URL.

        Args:
            value (list[str]): the candidate photo URLs.

        Returns:
            list[str]: the validated photo URLs.
        """
        validator = URLValidator(schemes=("http", "https"))
        for url in value:
            try:
                validator(url)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(
                    "Each photo must be a valid image URL."
                ) from exc
        return value


class QuestionCreateSerializer(serializers.Serializer):
    """Input for a customer asking a product question."""

    question = serializers.CharField(max_length=QUESTION_MAX_LENGTH)


class AnswerCreateSerializer(serializers.Serializer):
    """Input for a staff member answering a product question."""

    answer = serializers.CharField(max_length=ANSWER_MAX_LENGTH)


class ReviewModerationSerializer(serializers.ModelSerializer):
    """Staff-facing review shape with author and product context."""

    user = ReviewAuthorSerializer(read_only=True)
    product_slug = serializers.CharField(source="product.slug")
    product_name = serializers.CharField(source="product.name")
    verified_purchase = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            "id",
            "product_slug",
            "product_name",
            "rating",
            "title",
            "body",
            "photos",
            "verified_purchase",
            "is_approved",
            "user",
            "created_at",
        ]
        read_only_fields = fields

    def get_verified_purchase(self, obj):
        """Return whether the review carries a verified-purchase order line.

        Args:
            obj (Review): the review being serialized.

        Returns:
            bool: True when the review carries a verified-purchase line.
        """
        return obj.order_item_id is not None


class QuestionModerationSerializer(serializers.ModelSerializer):
    """Staff-facing question shape with author, product, and answers."""

    user = ReviewAuthorSerializer(read_only=True)
    product_slug = serializers.CharField(source="product.slug")
    product_name = serializers.CharField(source="product.name")
    answers = AnswerSerializer(many=True, read_only=True)

    class Meta:
        model = ProductQuestion
        fields = [
            "id",
            "product_slug",
            "product_name",
            "question",
            "is_approved",
            "user",
            "answers",
            "created_at",
        ]
        read_only_fields = fields
