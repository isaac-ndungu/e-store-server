"""Read-only query helpers for the reviews app.

Selectors encapsulate query construction so views and serializers never build
raw querysets directly. Public storefront reads select only approved rows and
use ``select_related``/``prefetch_related`` so list endpoints never trigger an
N+1 query against reviewers or answers; moderation reads are shaped for the
staff inbox.
"""

from django.db.models import Prefetch

from apps.catalog.models import Product
from apps.reviews.models import ProductAnswer, ProductQuestion, Review


def get_active_product_by_slug(slug):
    """Return an active, non-discontinued product by slug, or None.

    Uses ``only("pk")`` because the storefront review/question endpoints touch
    nothing but the primary key — the wide product row is not loaded.

    Args:
        slug (str): the product slug.

    Returns:
        Product | None: the product, or None when missing or hidden.
    """
    return (
        Product.objects.only("pk")
        .filter(slug=slug, is_active=True, is_discontinued=False)
        .first()
    )


def list_approved_reviews(product):
    """Return the storefront-visible reviews for a product, newest first.

    Photos are pre-fetched so the photo list nested in the response never
    triggers a per-review query.

    Args:
        product (Product): the product.

    Returns:
        QuerySet: the product's approved reviews with the author and photos
            fetched, ordered newest-first.
    """
    return (
        product.reviews.filter(is_approved=True)
        .select_related("user")
        .prefetch_related("photos")
        .order_by("-created_at", "-pk")
    )


def list_approved_questions(product):
    """Return the storefront-visible questions for a product with answers.

    Answers are pre-fetched with their authors in a single additional query so
    the list endpoint never fires per-question lookups.

    Args:
        product (Product): the product.

    Returns:
        QuerySet: the product's approved questions with the asker and answers
            pre-fetched, ordered newest-first.
    """
    return (
        product.questions.filter(is_approved=True)
        .select_related("user")
        .prefetch_related(
            Prefetch(
                "answers",
                queryset=ProductAnswer.objects.select_related("answered_by"),
            )
        )
        .order_by("-created_at", "-pk")
    )


def list_all_reviews(*, approved=None):
    """Return reviews for the staff moderation inbox.

    Args:
        approved (bool | None): when not None, restrict to reviews with that
            approval state.

    Returns:
        QuerySet: reviews with author, product, and photos fetched,
            newest-first.
    """
    queryset = (
        Review.objects.select_related("user", "product")
        .prefetch_related("photos")
        .order_by("-created_at", "-pk")
    )
    if approved is not None:
        queryset = queryset.filter(is_approved=approved)
    return queryset


def list_all_questions(*, approved=None):
    """Return questions for the staff moderation inbox.

    Args:
        approved (bool | None): when not None, restrict to questions with that
            approval state.

    Returns:
        QuerySet: questions with asker, product, and answers fetched,
            newest-first.
    """
    queryset = (
        ProductQuestion.objects.select_related("user", "product")
        .prefetch_related(
            Prefetch(
                "answers", queryset=ProductAnswer.objects.select_related("answered_by")
            )
        )
        .order_by("-created_at", "-pk")
    )
    if approved is not None:
        queryset = queryset.filter(is_approved=approved)
    return queryset


def get_review_for_staff(review_id):
    """Return a single review for moderation by id, or None.

    Args:
        review_id (int): the review primary key.

    Returns:
        Review | None: the review with author, product, order line, and photos
            fetched, or None when no review matches.
    """
    return (
        Review.objects.select_related("user", "product", "order_item")
        .prefetch_related("photos")
        .filter(pk=review_id)
        .first()
    )


def get_question_for_staff(question_id):
    """Return a single question for moderation by id, or None.

    Args:
        question_id (int): the question primary key.

    Returns:
        ProductQuestion | None: the question with asker, product, and answers
            fetched, or None when no question matches.
    """
    return (
        ProductQuestion.objects.select_related("user", "product")
        .prefetch_related(
            Prefetch(
                "answers", queryset=ProductAnswer.objects.select_related("answered_by")
            )
        )
        .filter(pk=question_id)
        .first()
    )
