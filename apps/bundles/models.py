"""Data models for the bundles app.

A dynamic ``Bundle`` groups several catalogue products (or specific variants)
that a shopper can buy together, priced as a set. Buying a bundle decomposes
into real per-component order lines at checkout with correct inventory and
tax treatment per component — never a single opaque line for the whole bundle
— which is why each ``BundleItem`` points at a specific component.

"""

from django.db import models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint

from apps.bundles.constants import (
    DISCOUNT_TYPE_CHOICES,
    DISCOUNT_TYPE_VALUES,
)


class Bundle(models.Model):
    """A purchasable set of catalogue components with a bundle-level discount.

    ``discount_type`` is either ``percent`` (a percentage off the regular
    total) or ``fixed`` (a fixed KES amount off). ``is_active`` is the staff
    override; ``starts_at`` / ``ends_at`` optionally bound when the bundle is
    offered. A bundle is only presented to the storefront when it is active
    and inside its window.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["slug"], name="bundle_slug_idx"),
            models.Index(fields=["is_active"], name="bundle_active_idx"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(discount_type__in=DISCOUNT_TYPE_VALUES)
                & Q(discount_value__gte=0),
                name="bundle_discount_type_valid_and_value_nonnegative",
            ),
            CheckConstraint(
                condition=Q(discount_type="percent") & Q(discount_value__lte=100)
                | Q(discount_type="fixed"),
                name="bundle_percent_discount_lte_100",
            ),
        ]

    def __str__(self):
        """Return the bundle name."""
        return self.name


class BundleItem(models.Model):
    """A single component of a bundle.

    ``product`` and, when the component is a specific colour/size/etc.,
    ``variant`` select the item. When ``variant`` is null the bundle matches
    the product broadly; quantity is the number of that component included.
    ``is_optional`` marks a component the shopper may drop. ``product`` is a
    plain ``ForeignKey`` because one product can appear in many bundles.

    ``product`` is ``CASCADE``: deleting a catalogue product silently removes
    it from every bundle that references it, which can leave those bundles
    unpriced. The storefront treats an unpriced bundle as not offerable (the
    price endpoint returns 404), but consider deactivating a product rather
    than deleting it when it appears in bundles.
    """

    bundle = models.ForeignKey(Bundle, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE)
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    quantity = models.PositiveIntegerField(default=1)
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]
        constraints = [
            CheckConstraint(
                condition=Q(quantity__gte=1),
                name="bundle_item_quantity_gte_1",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying bundle and component."""
        return f"{self.bundle_id} -> {self.product_id} (x{self.quantity})"
