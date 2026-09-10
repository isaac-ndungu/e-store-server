"""API serializers for the reviews app.

Read serializers expose only white-listed, storefront-safe fields: the
submitter's display name is shown, never their contact details, and
moderation shapes keep submitter and product context for staff.

Write serializers whitelist exactly what a caller may supply. A review names
a rating, the submitter's name and phone/email contact, an optional
headline/body, optional photo ids from the photo-upload endpoint, and an
optional order-line id for the verified-purchase badge. Photos are referenced
by id rather than URL, and the service only ever claims uploads from the
caller's own identity, so a random URL or a stranger's photo cannot be
planted on a review.
"""

from rest_framework import serializers

from apps.reviews.constants import (
    ANSWER_MAX_LENGTH,
    MAX_REVIEW_RATING,
    QUESTION_MAX_LENGTH,
    REVIEW_BODY_MAX_LENGTH,
    REVIEW_MAX_PHOTOS,
    REVIEW_TITLE_MAX_LENGTH,
)
from apps.reviews.models import ProductAnswer, ProductQuestion, Review, ReviewPhoto


class ReviewPhotoSerializer(serializers.ModelSerializer):
    """A review photo with its processed variants and display URL."""

    url = serializers.CharField(source="display_url")
    variants = serializers.JSONField(source="image_sources")

    class Meta:
        model = ReviewPhoto
        fields = ["id", "url", "variants"]
        read_only_fields = fields


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
    """Storefront-facing review shape with its processed photos."""

    photos = ReviewPhotoSerializer(many=True, read_only=True)
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
            "submitter_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_verified_purchase(self, obj):
        """Return whether the review is backed by a purchase order line.

        The badge simply reflects whether an ``OrderItem`` was claimed and
        validated at creation — the service guarantees that line matched the
        submitter's contact on a completed order.

        Args:
            obj (Review): the review being serialized.

        Returns:
            bool: True when the review carries a verified-purchase line.
        """
        return obj.order_item_id is not None


class QuestionSerializer(serializers.ModelSerializer):
    """Storefront-facing question shape with its answers oldest-first."""

    answers = AnswerSerializer(many=True, read_only=True)

    class Meta:
        model = ProductQuestion
        fields = ["id", "question", "submitter_name", "answers", "created_at"]
        read_only_fields = fields


class SubmitterSerializer(serializers.Serializer):
    """Name + contact identity shared by review and question submissions."""

    submitter_name = serializers.CharField(max_length=255)
    submitter_contact = serializers.CharField(max_length=255)


class ReviewCreateSerializer(SubmitterSerializer):
    """Input for posting a review.

    ``order_item_id`` optionally marks the review as a verified purchase; the
    service re-checks the line sits on a completed order matching the
    submitter's contact. ``photo_ids`` cites uploads produced by the
    photo-upload endpoint and capped at ``REVIEW_MAX_PHOTOS`` entries; the
    service only claims uploads from the caller's own identity.
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
    photo_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
        max_length=REVIEW_MAX_PHOTOS,
        default=list,
    )
    order_item_id = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )


class QuestionCreateSerializer(SubmitterSerializer):
    """Input for asking a product question."""

    question = serializers.CharField(max_length=QUESTION_MAX_LENGTH)


class AnswerCreateSerializer(serializers.Serializer):
    """Input for a staff member answering a product question."""

    answer = serializers.CharField(max_length=ANSWER_MAX_LENGTH)


class ReviewModerationSerializer(serializers.ModelSerializer):
    """Staff-facing review shape with submitter and product context."""

    product_slug = serializers.CharField(source="product.slug")
    product_name = serializers.CharField(source="product.name")
    photos = ReviewPhotoSerializer(many=True, read_only=True)
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
            "submitter_name",
            "submitter_contact",
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
    """Staff-facing question shape with submitter, product, and answers."""

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
            "submitter_name",
            "submitter_contact",
            "answers",
            "created_at",
        ]
        read_only_fields = fields
