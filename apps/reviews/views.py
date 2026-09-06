"""API views for the reviews app.

Storefront paths render reviews and Q&A outside the login wall (a deliberate
``IsAuthenticatedOrReadOnly`` — catalog browsing and social proof are public)
while creating a review, asking a question, or uploading a photo requires an
authenticated customer whose identity always comes from the token, never the
body. Moderation — approving/rejecting content and answering questions — is
gated behind the manager/support role.

Ownership: the only caller-supplied references to other resources are a
review's ``order_item_id`` and ``photo_ids``. The service resolves them
against the caller's own orders and the caller's own unattached uploads, and
returns a single opaque 400 for any miss, so the response never reveals
whether a given order line or photo exists or whom it belongs to.

All mutations go through the reviews service; no view writes a model field or
the product's rating aggregate directly.
"""

from functools import wraps
from pathlib import Path
from uuid import uuid4

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.db import DatabaseError
from django.http import Http404
from rest_framework import exceptions, permissions, serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.catalog.images import (
    delete_image_files,
    generate_variants,
    preferred_image_url,
)
from apps.catalog.validators import validate_image_upload
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.reviews.constants import (
    MAX_UNATTACHED_PHOTOS_PER_USER,
    REVIEW_PHOTO_MAX_SIZE_MB,
)
from apps.reviews.models import ReviewPhoto
from apps.reviews.selectors import (
    get_active_product_by_slug,
    get_question_for_staff,
    get_review_for_staff,
    list_all_questions,
    list_all_reviews,
    list_approved_questions,
    list_approved_reviews,
)
from apps.reviews.serializers import (
    AnswerCreateSerializer,
    QuestionCreateSerializer,
    QuestionModerationSerializer,
    QuestionSerializer,
    ReviewCreateSerializer,
    ReviewModerationSerializer,
    ReviewPhotoSerializer,
    ReviewSerializer,
)
from apps.reviews.services import (
    ReviewsDisabledError,
    _require_reviews_enabled,
    add_product_answer,
    create_product_question,
    create_review,
    delete_review_photo,
    set_question_approval,
    set_review_approval,
)


class ReviewsDisabledAPIException(exceptions.APIException):
    """Raised when the reviews feature is toggled off in the site settings.

    A disabled feature is server state, not a malformed request, so it must
    surface as ``503 Service Unavailable`` rather than the 400 a validation
    error would produce.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "reviews_disabled"


def _service_error_to_503(fn):
    """Wrap a service call so a disabled feature surfaces as HTTP 503.

    Lifts the service layer's ``ReviewsDisabledError`` into a DRF exception
    the exception handler renders as a 503, without merging it into the 400
    path used for business-rule validation errors. Any other exception
    propagates untouched.

    Args:
        fn (Callable): the service function to call.

    Returns:
        Callable: a wrapper that raises the 503 exception when the service
            reports the reviews feature as disabled.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ReviewsDisabledError as exc:
            raise ReviewsDisabledAPIException(str(exc)) from exc

    return wrapper


def _parse_approved_param(request):
    """Parse the optional ``approved`` moderation filter as a strict tri-state.

    Only the explicit true/false spellings act as filters; anything else is
    treated as absent so a garbage value never silently narrows the inbox to
    one approval state.

    Args:
        request: the HTTP request carrying the query string.

    Returns:
        bool | None: True/False to filter on that state, None for no filter.
    """
    raw = request.query_params.get("approved")
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised in ("true", "1", "yes"):
        return True
    if normalised in ("false", "0", "no"):
        return False
    return None


def _product_or_404(slug):
    """Resolve an active product by slug or raise HTTP 404.

    Args:
        slug (str): the product slug.

    Returns:
        Product: the resolved product.

    Raises:
        Http404: when the product is missing, inactive, or discontinued.
    """
    product = get_active_product_by_slug(slug)
    if product is None:
        raise Http404
    return product


def _staff_review_or_404(review_id):
    """Return the moderation review or raise HTTP 404.

    Args:
        review_id (int): the review primary key.

    Returns:
        Review: the matched review.

    Raises:
        Http404: when no review matches the id.
    """
    review = get_review_for_staff(review_id)
    if review is None:
        raise Http404
    return review


def _staff_question_or_404(question_id):
    """Return the moderation question or raise HTTP 404.

    Args:
        question_id (int): the question primary key.

    Returns:
        ProductQuestion: the matched question.

    Raises:
        Http404: when no question matches the id.
    """
    question = get_question_for_staff(question_id)
    if question is None:
        raise Http404
    return question


class ProductReviewsView(APIView):
    """List a product's reviews or create one as the caller.

    Listing is public by design — the storefront renders reviews outside the
    login wall; creating requires an authenticated customer. Read and write
    rate scopes differ, so ``throttle_scope`` resolves per method.
    """

    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    throttle_classes = [ScopedRateThrottle]

    @property
    def throttle_scope(self):
        """Pick the write-rate scope for creation, the read scope for listing.

        Returns:
            str: the DRF throttle scope for the current request method.
        """
        return "review_write" if self.request.method == "POST" else "review_read"

    def get(self, request, slug):
        """Return the product's approved reviews, newest first.

        Args:
            request: the GET request.
            slug (str): the product slug.

        Returns:
            Response: the paginated review list.
        """
        product = _product_or_404(slug)
        reviews = list_approved_reviews(product)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(reviews, request)
        serializer = ReviewSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, slug):
        """Create a review for the product as the authenticated caller.

        Args:
            request: the POST request carrying the review payload.
            slug (str): the product slug.

        Returns:
            Response: ``201 Created`` with the review, ``400`` for a
                business-rule failure, ``404`` when the product is not
                viewable, or ``503`` when the reviews feature is disabled.
        """
        product = _product_or_404(slug)
        input_serializer = ReviewCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        review = _service_error_to_503(_service_error_to_400(create_review))(
            user=request.user,
            product=product,
            rating=data["rating"],
            title=data.get("title", ""),
            body=data.get("body", ""),
            photo_ids=data.get("photo_ids", []),
            order_item_id=data.get("order_item_id"),
        )
        serializer = ReviewSerializer(review)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ProductQuestionsView(APIView):
    """List a product's questions or ask one as the caller.

    Listing is public by design; asking requires an authenticated customer.
    """

    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    throttle_classes = [ScopedRateThrottle]

    @property
    def throttle_scope(self):
        """Pick the write-rate scope for asking, the read scope for listing.

        Returns:
            str: the DRF throttle scope for the current request method.
        """
        return "review_write" if self.request.method == "POST" else "review_read"

    def get(self, request, slug):
        """Return the product's approved questions with answers, newest first.

        Args:
            request: the GET request.
            slug (str): the product slug.

        Returns:
            Response: the paginated question list.
        """
        product = _product_or_404(slug)
        questions = list_approved_questions(product)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(questions, request)
        serializer = QuestionSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, slug):
        """Ask a question about the product as the authenticated caller.

        Args:
            request: the POST request carrying the question text.
            slug (str): the product slug.

        Returns:
            Response: ``201 Created`` with the question, ``404`` when the
                product is not viewable, or ``503`` when the reviews feature
                is disabled.
        """
        product = _product_or_404(slug)
        input_serializer = QuestionCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        question = _service_error_to_503(
            _service_error_to_400(create_product_question)
        )(
            user=request.user,
            product=product,
            question=input_serializer.validated_data["question"],
        )
        serializer = QuestionSerializer(question)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ReviewPhotoUploadView(APIView):
    """Upload and process a single review photo as an authenticated customer.

    The image is validated by content (Pillow decode plus the allowed format
    set) and by size, then re-encoded into the responsive variant set. The
    resulting ``ReviewPhoto`` row is owned by the caller and stays unclaimed
    until it is attached to a review by id. Original uploads are never served
    to the storefront. Callers may hold at most
    ``MAX_UNATTACHED_PHOTOS_PER_USER`` unclaimed photos at once, which bounds
    how much storage the upload endpoint can consume ahead of a review.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "review_write"

    def post(self, request):
        """Accept a multipart image upload and return its processed output.

        Args:
            request: the multipart POST request carrying ``image``.

        Returns:
            Response: ``201 Created`` with the photo's id, display url, and
                variants; ``400`` for a missing, invalid, or oversized file or
                too many unclaimed uploads; or ``503`` when the reviews
                feature is disabled.
        """
        try:
            _require_reviews_enabled()
        except ReviewsDisabledError as exc:
            raise ReviewsDisabledAPIException(str(exc)) from exc
        uploaded = request.FILES.get("image")
        if uploaded is None:
            raise serializers.ValidationError({"image": "This field is required."})
        try:
            validate_image_upload(uploaded, max_size_mb=REVIEW_PHOTO_MAX_SIZE_MB)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"image": exc.messages}) from exc

        unclaimed_count = ReviewPhoto.objects.filter(
            user=request.user, review__isnull=True
        ).count()
        if unclaimed_count >= MAX_UNATTACHED_PHOTOS_PER_USER:
            raise serializers.ValidationError(
                {"image": "Delete existing uploaded photos before uploading more."}
            )

        extension = Path(uploaded.name).suffix or ".jpg"
        storage_name = default_storage.save(
            f"reviews/photos/{uuid4().hex}{extension}", uploaded
        )
        try:
            variants = generate_variants(storage_name)
            display_url = preferred_image_url(
                storage_name, variants
            ) or default_storage.url(storage_name)
            photo = ReviewPhoto.objects.create(
                user=request.user,
                storage_name=storage_name,
                image_sources=variants,
                display_url=display_url,
            )
        except OSError, ValueError, DatabaseError:
            delete_image_files(storage_name)
            raise
        serializer = ReviewPhotoSerializer(photo)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ReviewPhotoDeleteView(APIView):
    """Delete one of the caller's own unattached uploaded photos.

    Ownership is enforced in ``get_object``: a photo that does not belong to
    the caller is indistinguishable from one that does not exist, so a missing
    or foreign id collapses into the same opaque 404. A photo already attached
    to a review is published content and is rejected with a 400 instead.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "review_write"

    def get_object(self, request, photo_id):
        """Resolve a photo belonging to the caller or raise HTTP 404.

        Args:
            request: the HTTP request.
            photo_id (int): the photo primary key.

        Returns:
            ReviewPhoto: the caller's own photo.

        Raises:
            Http404: when the photo does not exist or belongs to another user.
        """
        photo = ReviewPhoto.objects.filter(pk=photo_id, user=request.user).first()
        if photo is None:
            raise Http404
        return photo

    def delete(self, request, photo_id):
        """Delete the caller's own unattached photo.

        Args:
            request: the DELETE request.
            photo_id (int): the photo primary key.

        Returns:
            Response: ``204 No Content`` when deleted, ``400`` when the photo
                is already attached to a review, or ``404`` when the photo is
                missing or not the caller's own.
        """
        photo = self.get_object(request, photo_id)
        _service_error_to_400(delete_review_photo)(photo=photo)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReviewModerationListView(APIView):
    """List every review for moderation by managers and support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request):
        """Return reviews filtered by the optional ``approved`` query flag.

        Args:
            request: the GET request (``?approved=true|false``).

        Returns:
            Response: the paginated review list for moderation.
        """
        approved = _parse_approved_param(request)
        reviews = list_all_reviews(approved=approved)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(reviews, request)
        serializer = ReviewModerationSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class ReviewApproveView(APIView):
    """Approve a review as staff, refreshing the product rating."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, review_id):
        """Mark the review approved.

        Args:
            request: the POST request.
            review_id (int): the review primary key.

        Returns:
            Response: ``200 OK`` with the moderation shape of the review.
        """
        review = _staff_review_or_404(review_id)
        updated = set_review_approval(review=review, approved=True)
        serializer = ReviewModerationSerializer(updated)
        return Response(serializer.data)


class ReviewRejectView(APIView):
    """Hide a review from the storefront as staff, refreshing the rating."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, review_id):
        """Mark the review unapproved.

        Args:
            request: the POST request.
            review_id (int): the review primary key.

        Returns:
            Response: ``200 OK`` with the moderation shape of the review.
        """
        review = _staff_review_or_404(review_id)
        updated = set_review_approval(review=review, approved=False)
        serializer = ReviewModerationSerializer(updated)
        return Response(serializer.data)


class QuestionModerationListView(APIView):
    """List every question for moderation by managers and support staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get(self, request):
        """Return questions filtered by the optional ``approved`` query flag.

        Args:
            request: the GET request (``?approved=true|false``).

        Returns:
            Response: the paginated question list for moderation.
        """
        approved = _parse_approved_param(request)
        questions = list_all_questions(approved=approved)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(questions, request)
        serializer = QuestionModerationSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class QuestionAnswerCreateView(APIView):
    """Answer a product question as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, question_id):
        """Add a staff answer, approving the question in the process.

        Args:
            request: the POST request carrying the answer text.
            question_id (int): the question primary key.

        Returns:
            Response: ``201 Created`` with the updated question moderation
                shape, or ``404`` when no question matches.
        """
        question = _staff_question_or_404(question_id)
        input_serializer = AnswerCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        add_product_answer(
            question=question,
            user=request.user,
            answer=input_serializer.validated_data["answer"],
        )
        updated = get_question_for_staff(question.pk)
        serializer = QuestionModerationSerializer(updated)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class QuestionApproveView(APIView):
    """Approve a question as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, question_id):
        """Mark the question approved.

        Args:
            request: the POST request.
            question_id (int): the question primary key.

        Returns:
            Response: ``200 OK`` with the moderation shape of the question.
        """
        question = _staff_question_or_404(question_id)
        updated = set_question_approval(question=question, approved=True)
        serializer = QuestionModerationSerializer(updated)
        return Response(serializer.data)


class QuestionRejectView(APIView):
    """Hide a question and its answers from the storefront as staff."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def post(self, request, question_id):
        """Mark the question unapproved.

        Args:
            request: the POST request.
            question_id (int): the question primary key.

        Returns:
            Response: ``200 OK`` with the moderation shape of the question.
        """
        question = _staff_question_or_404(question_id)
        updated = set_question_approval(question=question, approved=False)
        serializer = QuestionModerationSerializer(updated)
        return Response(serializer.data)
