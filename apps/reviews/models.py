"""Data models for the reviews app.

Customer reviews, product questions, and staff answers power the trust
surfaces on the storefront. The aggregate rating and review count are
denormalised onto ``catalog.Product`` (``average_rating``/``review_count``)
and maintained exclusively by this app's service layer, so the reader never
touches the review table for a product listing; the review and Q&A threads
themselves live here.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint

from apps.reviews.constants import MAX_REVIEW_RATING


class Review(models.Model):
    """A customer's rating and written review of a product.

    ``user`` is required — reviews come from registered accounts, which is
    what gives every review a stable identity for moderation and for the
    per-buyer single-review rule. ``order_item`` is optional: when present it
    marks the purchase as verified (the line must belong to the reviewer's own
    completed order for that product) and drives the storefront's
    "Verified Purchase" badge.

    ``is_approved`` gates storefront visibility. New reviews default to
    approved and staff can hide one later; the product's denormalised rating
    aggregate always counts only approved reviews.
    """

    product = models.ForeignKey(
        "catalog.Product",
        related_name="reviews",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="reviews",
        on_delete=models.CASCADE,
    )
    order_item = models.ForeignKey(
        "orders.OrderItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviews",
    )
    rating = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    photos = models.JSONField(default=list, blank=True)
    is_approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            CheckConstraint(
                condition=Q(rating__gte=1) & Q(rating__lte=MAX_REVIEW_RATING),
                name="rev_rating_in_range",
            ),
            models.UniqueConstraint(
                fields=["user", "product"],
                name="rev_one_review_per_user_product",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product", "is_approved", "-created_at"],
                name="rev_product_approved_idx",
            ),
            models.Index(fields=["order_item"], name="rev_order_item_idx"),
            models.Index(fields=["user", "-created_at"], name="rev_user_created_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the review."""
        return f"review of product {self.product_id} by user {self.user_id} ({self.rating}/5)"


class ProductQuestion(models.Model):
    """A customer's question about a product on the storefront.

    ``is_approved`` gates visibility exactly as on ``Review``. Questions are
    answered by staff through ``ProductAnswer``; adding an answer also approves
    the question so the thread becomes storefront-visible together.
    """

    product = models.ForeignKey(
        "catalog.Product",
        related_name="questions",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="product_questions",
        on_delete=models.CASCADE,
    )
    question = models.TextField()
    is_approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(
                fields=["product", "is_approved", "-created_at"],
                name="pq_product_approved_idx",
            ),
            models.Index(fields=["user", "-created_at"], name="pq_user_created_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the question."""
        return f"question {self.pk} about product {self.product_id}"


class ProductAnswer(models.Model):
    """A staff answer on a product question.

    ``is_staff_answer`` records who answered so the storefront can style buyer
    questions and staff answers differently. Answers always belong to a
    question; hiding the question hides the whole thread.
    """

    question = models.ForeignKey(
        ProductQuestion,
        related_name="answers",
        on_delete=models.CASCADE,
    )
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="product_answers",
    )
    is_staff_answer = models.BooleanField(default=False)
    answer = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(
                fields=["question", "created_at"], name="pa_question_created_idx"
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the answer."""
        return f"answer {self.pk} on question {self.question_id}"
