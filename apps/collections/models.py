"""Data models for the collections app.

Collections group products for storefront display. A ``Collection`` is either
``manual`` (a curated, staff-maintained list held as ``CollectionMembership``
rows) or ``smart`` (auto-updating from a rule — new arrivals, recently
restocked, on sale, best sellers, or almost gone). Smart membership is
recomputed periodically by a background job and the resulting product list is
cached per collection slug, so the storefront renders fast and a manual
curator never has to hand-edit membership.

``CollectionMembership`` uses a plain ``ForeignKey`` to ``catalog.Product``
(not a one-to-one): a product may appear in many collections, and one
collection many products. The pair is unique per collection, so a product
cannot be listed twice in the same collection.
"""

from django.db import models
from django.db.models import Q

from apps.collections.constants import SMART_RULE_CHOICES_VALUES


class Collection(models.Model):
    """A named grouping of products for storefront display.

    ``collection_type`` distinguishes a manually curated list from a rule-driven
    one. A smart collection carries exactly one ``smart_rule`` and its window/
    threshold arguments; a manual one carries none. ``last_refreshed_at``
    records when a smart collection's membership was last recomputed, so
    staff can tell at a glance whether the automatic refresh is healthy.
    ``starts_at`` / ``ends_at``
    optionally bound when the collection is active, and ``is_active`` is the
    staff override — an inactive collection is hidden from the storefront even
    within its window. ``display_location`` and ``sort_order`` are presentation
    hints for where and in what order the collection renders.
    """

    COLLECTION_TYPE_CHOICES = (
        ("manual", "Manual — curated list"),
        ("smart", "Smart — rule-based, auto-updating"),
    )
    SMART_RULE_CHOICES = (
        ("new_arrivals", "New Arrivals"),
        ("restocked", "Recently Restocked"),
        ("on_sale", "On Sale"),
        ("best_sellers", "Best Sellers"),
        ("low_stock", "Almost Gone"),
    )

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    collection_type = models.CharField(
        max_length=10, choices=COLLECTION_TYPE_CHOICES, default="manual"
    )
    smart_rule = models.CharField(max_length=20, choices=SMART_RULE_CHOICES, blank=True)
    rule_window_days = models.PositiveIntegerField(default=14)
    rule_threshold = models.PositiveIntegerField(null=True, blank=True)
    banner_image = models.ImageField(upload_to="collections/banners/", blank=True)
    description = models.TextField(blank=True)
    display_location = models.CharField(max_length=50, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["slug"], name="coll_slug_idx"),
            models.Index(fields=["is_active"], name="coll_active_idx"),
            models.Index(fields=["collection_type"], name="coll_type_idx"),
            models.Index(fields=["smart_rule"], name="coll_rule_idx"),
            models.Index(fields=["display_location"], name="coll_loc_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(collection_type="manual")
                | Q(smart_rule__in=SMART_RULE_CHOICES_VALUES),
                name="coll_smart_rule_requires_smart_type",
            ),
        ]

    def __str__(self):
        """Return the collection name with its type."""
        return f"{self.name} ({self.get_collection_type_display()})"


class CollectionMembership(models.Model):
    """A product belonging to a collection.

    For manual collections this row is the curator's explicit choice; for
    smart collections it holds the last computed membership until the next
    refresh replaces it. ``sort_order`` controls display sequence within the
    collection. ``product`` is a ``ForeignKey`` so one product can belong to
    many collections; the ``(collection, product)`` pair is unique so a
    product never appears twice in the same collection.
    """

    collection = models.ForeignKey(
        Collection, related_name="memberships", on_delete=models.CASCADE
    )
    product = models.ForeignKey(
        "catalog.Product",
        related_name="collection_memberships",
        on_delete=models.CASCADE,
    )
    added_at = models.DateTimeField(auto_now_add=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]
        unique_together = ("collection", "product")
        indexes = [
            models.Index(fields=["product"], name="cm_product_idx"),
            models.Index(fields=["collection", "sort_order"], name="cm_coll_sort_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying collection and product."""
        return f"{self.collection_id} -> product {self.product_id}"
