"""Data models for the social proof app.

The durable log of product page views (``ProductViewEvent``) is the analytics
backbone behind the storefront's urgency signals. Each row records one view of
a product by one browsing session. The live "currently viewing" count rendered
next to a product is served from Redis (``apps.social_proof.cache``), not by
aggregating these rows, so the hot urgency read never touches this table;
``ProductViewEvent`` is an append-only history for reporting and admin review
rather than a query target for the storefront.
"""

from django.db import models
from django.utils import timezone


class ProductViewEvent(models.Model):
    """One recorded product view by one browsing session.

    ``product`` is a ``ForeignKey`` because a product can be viewed many times
    by many sessions, and one session can view a product repeatedly, so there
    is intentionally no per-view uniqueness contract.

    ``bucket_started_at`` is the start of the five-minute bucket the view fell
    into; together with ``product`` and ``session_key`` it is unique, which
    collapses the repeated views a single session fires in quick succession
    into one durable row. Without the bucket the same session's page-load
    retries would each append a row, over-weighting analytics in favour of
    flaky connections. The live "currently viewing" count shoppers see is a
    Redis counter maintained in ``apps.social_proof.cache``; this table is the
    durable history behind it, and ``created_at`` carries the view timestamp.
    """

    product = models.ForeignKey(
        "catalog.Product",
        related_name="view_events",
        on_delete=models.CASCADE,
    )
    session_key = models.CharField(max_length=100)
    bucket_started_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product", "created_at"], name="spv_product_time_idx"),
            models.Index(fields=["created_at"], name="spv_created_idx"),
            models.Index(fields=["session_key", "created_at"], name="spv_session_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "session_key", "bucket_started_at"],
                name="spv_unique_view_per_bucket",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the recorded view."""
        return f"view of product {self.product_id} by session {self.session_key}"
