"""Business logic for the reviews app.

The service layer is the only place ``Review``, ``ProductQuestion``, and
``ProductAnswer`` rows are created or moderated, and the only place the
product's denormalised ``average_rating``/``review_count`` are recomputed, so
the storefront rating read never drifts from what reviewers actually wrote.

Invariants upheld here:

- Free-form text (submitter name, review title/body, question text, answer
  text) is sanitised with ``bleach`` at this boundary, so markup and event
  handlers are stripped before the text is stored and later rendered on the
  storefront.
- Submissions are anonymous: the public identity is the submitted name and
  contact, never an account. One review is allowed per contact per product; a
  second attempt is rejected with a clean error.
- A review may reference at most one ``OrderItem`` as proof of purchase. The
  line must be for the same product, sit on a completed order, and match the
  submitter's contact (order phone or email) — and must not already be
  claimed by an earlier review.
- Photos attach by ``ReviewPhoto`` id: each id must be an unattached upload
  from the same identity (the authenticated user, or the same guest session
  key), so no reviewer can reference a stranger's photo.
- Rating aggregates count only approved reviews. New submissions default to
  hidden; every create, approval change, or review deletion recomputes the
  aggregate in the same transaction, and the product row is locked during the
  recompute so concurrent review writes to the same product cannot commit a
  stale aggregate.
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


def _resolve_verified_order_item(submitter_contact, product, order_item_id):
    """Resolve an order line as proof of purchase for a review.

    The line must be for the reviewed product, sit on an order in a completed
    state, and match the submitter's contact — the order's phone (compared
    after normalization) or email (compared case-insensitively). A line that
    is missing, foreign, for a different product, contact-mismatched, or on a
    not-completed order all collapse into the same opaque error so the
    response never reveals whether a given order-line id exists or who owns
    it. An already-claimed line is rejected so one purchase can verify only
    one review.

    Args:
        submitter_contact (str): the reviewer's phone or email.
        product (Product): the reviewed product.
        order_item_id (int): the candidate order-line primary key.

    Returns:
        OrderItem: the qualifying, unclaimed order line.

    Raises:
        ValidationError: when the line does not qualify as this submitter's
            verified purchase, or is already claimed by another review.
    """
    from apps.accounts.services import normalize_email, normalize_phone_number

    contact = (submitter_contact or "").strip()
    candidates = (
        OrderItem.objects.select_related("order")
        .filter(
            pk=order_item_id,
            product=product,
            order__status__in=VERIFIED_PURCHASE_STATUSES,
        )
        .first()
    )
    match = False
    if candidates is not None:
        if "@" in contact:
            match = candidates.order.email and (
                normalize_email(candidates.order.email) == normalize_email(contact)
            )
        elif contact:
            match = normalize_phone_number(
                candidates.order.phone
            ) == normalize_phone_number(contact)
    if not match:
        raise ValidationError(
            "The selected purchase does not qualify as a verified purchase."
        )
    if Review.objects.filter(order_item=candidates).exists():
        raise ValidationError("That purchase has already been used to verify a review.")
    return candidates


def _resolve_photo_ids(user, session_key, photo_ids):
    """Resolve photo ids into claimable uploads from the same identity.

    Every id must reference an unattached upload from the caller's own
    identity: the authenticated ``user``, or the guest ``session_key`` when
    anonymous. A foreign, already-attached, or duplicated id makes the whole
    set invalid so a reviewer can never attach somebody else's photo or reuse
    one across two reviews.

    Args:
        user (User | None): the authenticated reviewer, if any.
        session_key (str): the guest session key, when anonymous.
        photo_ids (list[int]): the candidate photo primary keys.

    Returns:
        list[ReviewPhoto]: the claimable, unclaimed uploads.

    Raises:
        ValidationError: when any id is not the caller's own unclaimed upload.
    """
    ids = list(dict.fromkeys(photo_ids))
    if len(ids) != len(photo_ids):
        raise ValidationError("Duplicate photos are not allowed.")
    if not ids:
        return []
    if user is not None and user.is_authenticated:
        lookup = {"user": user}
    elif session_key:
        lookup = {"user__isnull": True, "session_key": session_key}
    else:
        raise ValidationError("Each photo must be your own unclaimed upload.")
    photos = list(ReviewPhoto.objects.filter(pk__in=ids, review__isnull=True, **lookup))
    if len(photos) != len(ids):
        raise ValidationError("Each photo must be your own unclaimed upload.")
    return photos


def create_review(
    *,
    user=None,
    product,
    rating,
    submitter_name=None,
    submitter_contact=None,
    session_key="",
    title="",
    body="",
    photo_ids=None,
    order_item_id=None,
):
    """Create an anonymous review for a product, pending staff approval.

    The public identity is the submitted name/contact, never an account. One
    review is allowed per contact per product (per account per product when an
    authenticated user submits). A review may verify a purchase by referencing
    an ``OrderItem`` whose order phone/email matches the submitter's contact.
    Photos are claimed as the review is created — each id must be an
    unattached upload from the same identity. The review starts hidden; staff
    approval surfaces it and recomputes the product aggregate.

    Args:
        user (User | None): the authenticated submitter, if any (recorded for
            audit, not displayed).
        product (Product): the reviewed product.
        rating (int): the star rating within the configured scale.
        submitter_name (str): the public display name.
        submitter_contact (str): the submitter's phone or email.
        session_key (str): the guest session key, when anonymous.
        title (str): an optional short headline.
        body (str): an optional written review.
        photo_ids (list[int] | None): ids of the caller's own unattached
            uploads to attach, if any.
        order_item_id (int | None): a completed-purchase order line proving
            the review is verified, if any.

    Returns:
        Review: the created, unapproved review.

    Raises:
        ReviewsDisabledError: if the reviews feature is toggled off.
        ValidationError: if the rating is out of range, the name/contact is
            missing, the contact already reviewed the product, the referenced
            purchase is not a qualifying verified purchase, or any photo id
            is not the caller's own unclaimed upload.
    """
    _require_reviews_enabled()
    photo_ids = photo_ids or []
    if not MIN_REVIEW_RATING <= rating <= MAX_REVIEW_RATING:
        raise ValidationError(
            f"Rating must be between {MIN_REVIEW_RATING} and {MAX_REVIEW_RATING}."
        )
    name = _sanitize(submitter_name or "").strip()
    contact = (submitter_contact or "").strip()
    if not name:
        raise ValidationError("A name is required to post a review.")
    if not contact:
        raise ValidationError("A phone number or email is required to post a review.")

    if user is not None and user.is_authenticated:
        if Review.objects.filter(user=user, product=product).exists():
            raise ValidationError("You have already reviewed this product.")
    elif Review.objects.filter(
        product=product, submitter_contact__iexact=contact
    ).exists():
        raise ValidationError("You have already reviewed this product.")

    with transaction.atomic():
        order_item = None
        if order_item_id is not None:
            order_item = _resolve_verified_order_item(contact, product, order_item_id)
        photos = _resolve_photo_ids(user, session_key, photo_ids)
        try:
            review = Review.objects.create(
                product=product,
                user=user if user is not None and user.is_authenticated else None,
                submitter_name=name,
                submitter_contact=contact,
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


def create_product_question(
    *, user=None, product, question, submitter_name=None, submitter_contact=None
):
    """Ask a question about a product anonymously, pending staff approval.

    The question text is sanitised and the question starts hidden so spam
    never reaches the storefront before a staff member sees it.

    Args:
        user (User | None): the authenticated asker, if any (audit only).
        product (Product): the product being asked about.
        question (str): the question text.
        submitter_name (str): the public display name.
        submitter_contact (str): the asker's phone or email.

    Returns:
        ProductQuestion: the created, unapproved question.

    Raises:
        ReviewsDisabledError: when the reviews feature is disabled.
        ValidationError: when the name or contact is missing.
    """
    _require_reviews_enabled()
    name = _sanitize(submitter_name or "").strip()
    contact = (submitter_contact or "").strip()
    if not name:
        raise ValidationError("A name is required to ask a question.")
    if not contact:
        raise ValidationError("A phone number or email is required to ask a question.")
    return ProductQuestion.objects.create(
        product=product,
        user=user if user is not None and user.is_authenticated else None,
        submitter_name=name,
        submitter_contact=contact,
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
