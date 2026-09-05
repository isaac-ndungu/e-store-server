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
- Photos attach by ``ReviewPhoto`` id: each id must be an unattached upload of
  the reviewer's own, so no reviewer can reference a stranger's photo.
- Rating aggregates count only approved reviews. Every create, approval
  change, or review deletion recomputes them in the same transaction, and the
  product row is locked during the recompute so concurrent review writes to
  the same product cannot commit a stale aggregate.
"""

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
from apps.reviews.models import ProductAnswer, ProductQuestion, Review, ReviewPhoto

_RATING_QUANT = Decimal("0.01")


class ReviewsDisabledError(Exception):
    """Raised when the reviews feature is toggled off in the site settings.

    Distinct from a validation error so views can answer with a service-status
    response (503) rather than claim the request was malformed.
    """


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
        ReviewsDisabledError: when the reviews feature is disabled.
    """
    if not is_feature_enabled():
        raise ReviewsDisabledError(
            "Posting reviews and questions is currently disabled."
        )


def _recompute_rating(product_id):
    """Recalculate and store the product's rating aggregate from approved views.

    Computes the average rating and count over approved reviews and writes
    them to the ``Product`` row without a full model save. The product row is
    locked first so concurrent review writes to the same product serialize
    here: each writer re-reads the committed review set before publishing its
    aggregate, so the storefront number can never be left counting a stale
    review set.

    Args:
        product_id (int): the primary key of the reviewed product.

    Returns:
        tuple[Decimal, int]: the new average rating and review count.
    """
    with transaction.atomic():
        Product.objects.select_for_update().only("pk").get(pk=product_id)
        stats = Review.objects.filter(
            product_id=product_id, is_approved=True
        ).aggregate(
            average=Avg("rating"),
            count=Count("id"),
        )
        average = Decimal("0.00")
        if stats["average"] is not None:
            average = Decimal(str(stats["average"])).quantize(
                _RATING_QUANT, rounding=ROUND_HALF_UP
            )
        count = stats["count"] or 0
        Product.objects.filter(pk=product_id).update(
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


def _resolve_photo_ids(user, photo_ids):
    """Resolve a list of photo ids into claimable uploads belonging to the caller.

    Every id must reference an unattached upload owned by ``user``. A foreign,
    already-attached, or duplicated id makes the whole set invalid so a
    reviewer can never attach somebody else's photo or reuse one across two
    reviews.

    Args:
        user (User): the authenticated reviewer.
        photo_ids (list[int]): the candidate photo primary keys.

    Returns:
        list[ReviewPhoto]: the claimable, unclaimed uploads.

    Raises:
        ValidationError: when any id is not the caller's own unclaimed upload.
    """
    ids = list(dict.fromkeys(photo_ids))
    if len(ids) != len(photo_ids):
        raise ValidationError("Duplicate photos are not allowed.")
    photos = list(
        ReviewPhoto.objects.filter(pk__in=ids, user=user, review__isnull=True)
    )
    if len(photos) != len(ids):
        raise ValidationError("Each photo must be your own unclaimed upload.")
    return photos


def create_review(
    *,
    user,
    product,
    rating,
    title="",
    body="",
    photo_ids=None,
    order_item_id=None,
):
    """Create a customer review for a product.

    A review may verify a purchase by referencing an ``OrderItem``; the line
    is validated against the caller's own completed orders and may only verify
    one review. Photos are claimed as the review is created — each id must be
    the caller's own unattached upload. One review is allowed per user per
    product, and the product's rating aggregate is recomputed in the same
    transaction as the create, so the aggregate and the review set are never
    observed in different states.

    Args:
        user (User): the authenticated reviewer.
        product (Product): the reviewed product.
        rating (int): the star rating within the configured scale.
        title (str): an optional short headline.
        body (str): an optional written review.
        photo_ids (list[int] | None): ids of the reviewer's own unattached
            uploads to attach, if any.
        order_item_id (int | None): a completed-purchase order line proving
            the review is verified, if any.

    Returns:
        Review: the created, approved review.

    Raises:
        ReviewsDisabledError: if the reviews feature is toggled off.
        ValidationError: if the rating is out of range, the caller already
            reviewed the product, the referenced purchase is not a qualifying
            verified purchase, or any photo id is not the caller's own
            unclaimed upload.
    """
    _require_reviews_enabled()
    photo_ids = photo_ids or []
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
        photos = _resolve_photo_ids(user, photo_ids)
        try:
            review = Review.objects.create(
                product=product,
                user=user,
                order_item=order_item,
                rating=rating,
                title=_sanitize(title),
                body=_sanitize(body),
            )
        except IntegrityError:
            raise ValidationError("You have already reviewed this product.") from None
        if photos:
            ReviewPhoto.objects.filter(pk__in=[photo.pk for photo in photos]).update(
                review=review
            )
        _recompute_rating(product.pk)
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
        ReviewsDisabledError: when the reviews feature is disabled.
    """
    _require_reviews_enabled()
    return ProductQuestion.objects.create(
        product=product,
        user=user,
        question=_sanitize(question),
    )


def delete_review_photo(*, photo):
    """Delete an unattached uploaded photo along with its stored files.

    The photo row is removed and its ``post_delete`` signal deletes the stored
    original and processed variants. A photo already attached to a review is
    published content and is not removable through this route; ownership is
    enforced by the calling view before this service is reached.

    Args:
        photo (ReviewPhoto): the unattached upload to delete.

    Returns:
        None

    Raises:
        ValidationError: when the photo is already attached to a review.
    """
    if photo.review_id is not None:
        raise ValidationError("A photo attached to a review cannot be deleted.")
    photo.delete()


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
        _recompute_rating(review.product_id)
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
