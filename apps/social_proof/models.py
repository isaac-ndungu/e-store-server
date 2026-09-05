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


class ProductViewEvent(models.Model):
    """One recorded product view by one browsing session.

    ``product`` is a ``ForeignKey`` because a product can be viewed many times
    by many sessions, and one session can view a product repeatedly, so there
    is intentionally no uniqueness contract beyond the row itself — every view
    is an event worth keeping.

    The live-viewer count shoppers see is a Redis counter maintained in
    ``apps.social_proof.cache``; this table is the durable history behind it.
    ``created_at`` carries the auto timestamp of the view.
    """

    product = models.ForeignKey(
        "catalog.Product",
        related_name="view_events",
        on_delete=models.CASCADE,
    )
    session_key = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product", "created_at"], name="spv_product_time_idx"),
            models.Index(fields=["created_at"], name="spv_created_idx"),
            models.Index(fields=["session_key", "created_at"], name="spv_session_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the recorded view."""
        return f"view of product {self.product_id} by session {self.session_key}"
