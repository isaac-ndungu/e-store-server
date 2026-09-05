"""Business logic for the reviews app.

The service layer is the only place ``Review``, ``ProductQuestion``, and
``ProductAnswer`` rows are created or moderated, and the only place the
product's denormalised ``average_rating``/``review_count`` are recomputed, so
the storefront rating read never drifts from what reviewers actually wrote.

Invariants upheld here:

- Free-form text (review title/body, question text, answer text) is sanitised
  with ``bleach`` at this boundary, so markup and event handlers are stripped
  before the text is stored and later rendered on the storefront.
- A review may reference at most one ``OrderItem`` as proof of purchase. The
  line must belong to the reviewer's own order (``order.user`` must match the
  authenticated caller), must be for the same product, must sit on a completed
  order, and must not already be claimed by an earlier review.
- One review is allowed per buyer per product; a second attempt is rejected
  with a clean error rather than surfaced as an integrity violation.
- Rating aggregates count only approved reviews, and every create or
  moderation transition recomputes them in the same transaction.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

import bleach
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count

from apps.catalog.models import Product
from apps.orders.models import OrderItem
from apps.reviews.cache import is_feature_enabled
from apps.reviews.constants import (
    MAX_REVIEW_RATING,
    MIN_REVIEW_RATING,
    VERIFIED_PURCHASE_STATUSES,
)
from apps.reviews.models import ProductAnswer, ProductQuestion, Review

logger = logging.getLogger(__name__)

_RATING_QUANT = Decimal("0.01")


def _sanitize(value):
    """Strip markup from free-form text before it is stored.

    ``bleach`` removes disallowed tags and their attributes while keeping the
    textual content, so a reviewer's honest prose survives while embedded
    scripts, iframes, and inline event handlers are dropped.

    Args:
        value (str): the raw text submitted by a customer or staff member.

    Returns:
        str: the sanitised text.
    """
    return bleach.clean(value, tags=set(), strip=True)


def _require_reviews_enabled():
    """Raise when the reviews feature is toggled off in the site settings.

    Reads the cached ``enable_reviews`` flag; a disabled feature stops new
    reviews and questions from being posted while already-published content
    stays readable.

    Raises:
        ValidationError: when the reviews feature is disabled.
    """
    if not is_feature_enabled():
        raise ValidationError("Posting reviews and questions is currently disabled.")


def _recompute_rating(product):
    """Recalculate and store the product's rating aggregate from approved views.

    Computes the average rating and count over approved reviews and writes
    them to the ``Product`` row without a full model save, so the aggregate is
    always in step with the visible review set. Called inside the transaction
    of every create and moderation change.

    Args:
        product (Product): the reviewed product.

    Returns:
        tuple[Decimal, int]: the new average rating and review count.
    """
    stats = Review.objects.filter(product=product, is_approved=True).aggregate(
        average=Avg("rating"),
        count=Count("id"),
    )
    average = Decimal("0.00")
    if stats["average"] is not None:
        average = Decimal(str(stats["average"])).quantize(
            _RATING_QUANT, rounding=ROUND_HALF_UP
        )
    count = stats["count"] or 0
    Product.objects.filter(pk=product.pk).update(
        average_rating=average,
        review_count=count,
    )
    return average, count


def _resolve_verified_order_item(user, product, order_item_id):
    """Resolve an order line as proof of purchase for a review.

    The line must belong to the reviewer's own order, be for the reviewed
    product, and sit on an order in a completed state. A line that is missing,
    foreign, for a different product, or on a not-completed order all collapse
    into the same opaque error so the response never reveals whether a given
    order-line id exists or who owns it. An already-claimed line is rejected
    so one purchase can verify only one review.

    Args:
        user (User): the authenticated reviewer.
        product (Product): the reviewed product.
        order_item_id (int): the candidate order-line primary key.

    Returns:
        OrderItem: the qualifying, unclaimed order line.

    Raises:
        ValidationError: when the line does not qualify as this reviewer's
            verified purchase, or is already claimed by another review.
    """
    order_item = (
        OrderItem.objects.select_related("order")
        .filter(
            pk=order_item_id,
            product=product,
            order__user=user,
            order__status__in=VERIFIED_PURCHASE_STATUSES,
        )
        .first()
    )
    if order_item is None:
        raise ValidationError(
            "The selected purchase does not qualify as a verified purchase."
        )
    if Review.objects.filter(order_item=order_item).exists():
        raise ValidationError("That purchase has already been used to verify a review.")
    return order_item


def create_review(
    *, user, product, rating, title="", body="", photos=None, order_item_id=None
):
    """Create a customer review for a product.

    A review may verify a purchase by referencing an ``OrderItem``; the line
    is validated against the caller's own completed orders and may only verify
    one review. One review is allowed per user per product, and the product's
    rating aggregate is recomputed in the same transaction as the create, so
    the aggregate and the review set are never observed in different states.

    Args:
        user (User): the authenticated reviewer.
        product (Product): the reviewed product.
        rating (int): the star rating within the configured scale.
        title (str): an optional short headline.
        body (str): an optional written review.
        photos (list[str] | None): validated image URLs from the photo-upload
            endpoint, if any.
        order_item_id (int | None): a completed-purchase order line proving
            the review is verified, if any.

    Returns:
        Review: the created, approved review.

    Raises:
        ValidationError: if reviews are disabled, the rating is out of range,
            the caller already reviewed the product, or the referenced
            purchase is not a qualifying verified purchase.
    """
    _require_reviews_enabled()
    photos = photos or []
    if not MIN_REVIEW_RATING <= rating <= MAX_REVIEW_RATING:
        raise ValidationError(
            f"Rating must be between {MIN_REVIEW_RATING} and {MAX_REVIEW_RATING}."
        )

    if Review.objects.filter(user=user, product=product).exists():
        raise ValidationError("You have already reviewed this product.")

    with transaction.atomic():
        order_item = None
        if order_item_id is not None:
            order_item = _resolve_verified_order_item(user, product, order_item_id)
        try:
            review = Review.objects.create(
                product=product,
                user=user,
                order_item=order_item,
                rating=rating,
                title=_sanitize(title),
                body=_sanitize(body),
                photos=photos,
            )
        except IntegrityError:
            raise ValidationError("You have already reviewed this product.") from None
        _recompute_rating(product)
    return review


def create_product_question(*, user, product, question):
    """Ask a question about a product as a customer.

    The question text is sanitised and the question defaults to approved, so a
    storefront staff answer can surface it or staff can hide it later.

    Args:
        user (User): the authenticated asker.
        product (Product): the product being asked about.
        question (str): the question text.

    Returns:
        ProductQuestion: the created question.

    Raises:
        ValidationError: when the reviews feature is disabled.
    """
    _require_reviews_enabled()
    return ProductQuestion.objects.create(
        product=product,
        user=user,
        question=_sanitize(question),
    )


def add_product_answer(*, question, user, answer):
    """Add a staff answer to a product question.

    Answering also approves the question, so a thread becomes storefront
    visible the moment the first staff reply lands rather than only after a
    separate moderation step.

    Args:
        question (ProductQuestion): the question being answered.
        user (User): the answering staff member.
        answer (str): the staff reply text.

    Returns:
        ProductAnswer: the created answer.
    """
    with transaction.atomic():
        answered = ProductAnswer.objects.create(
            question=question,
            answered_by=user,
            is_staff_answer=True,
            answer=_sanitize(answer),
        )
        if not question.is_approved:
            question.is_approved = True
            question.save(update_fields=["is_approved"])
    return answered


def set_review_approval(*, review, approved):
    """Approve or hide a review as staff and refresh the rating aggregate.

    Recomputes the product's average rating and count in the same transaction
    as the visibility change, so hiding a low rating instantly moves the
    storefront number.

    Args:
        review (Review): the review to moderate.
        approved (bool): True to show the review, False to hide it.

    Returns:
        Review: the updated review.
    """
    with transaction.atomic():
        review.is_approved = approved
        review.save(update_fields=["is_approved"])
        _recompute_rating(review.product)
    return review


def set_question_approval(*, question, approved):
    """Approve or hide a question (and its answers) as staff.

    Args:
        question (ProductQuestion): the question to moderate.
        approved (bool): True to show the question and its answers, False to
            hide the whole thread.

    Returns:
        ProductQuestion: the updated question.
    """
    question.is_approved = approved
    question.save(update_fields=["is_approved"])
    return question
