"""Data models for the cart app.

A ``Cart`` is an anonymous, server-persisted shopping cart identified by a
long-lived ``HttpOnly`` cookie carrying its ``anonymous_id`` — no login is
involved anywhere. The cookie survives page reloads, browser restarts, and
client-side storage wipes; it does not survive a device or browser switch,
since there is no account to tie it to.

Prices are never stored on cart lines. Every read reprices each line
server-side from the current catalogue/promotion state, so the cart always
shows what the customer would actually be charged at intake time.
"""

import uuid

from django.core.validators import MinValueValidator
from django.db import models


class Cart(models.Model):
    """An anonymous visitor's server-side cart.

    ``anonymous_id`` is the value stored in the visitor's cookie and is the
    only lookup key — carts are never tied to a user account. ``created_at``
    supports future abandonment analysis; ``updated_at`` refreshes on every
    line change.
    """

    anonymous_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["anonymous_id"], name="cart_anon_id_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the cart."""
        return f"Cart {self.anonymous_id}"


class CartItem(models.Model):
    """One line in an anonymous cart.

    ``variant`` is the purchasable configuration and is always set.
    ``bundle`` is set only when the line was added as part of a bundle offer,
    so intake can expand bundle pricing; it never changes what the line
    points at. ``quantity`` is capped at 999 to match the intake limit.
    """

    cart = models.ForeignKey(Cart, related_name="items", on_delete=models.CASCADE)
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.CASCADE, related_name="cart_items"
    )
    bundle = models.ForeignKey(
        "bundles.Bundle",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cart_items",
    )
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["cart"], name="cartitem_cart_idx"),
            models.Index(fields=["variant"], name="cartitem_variant_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="cartitem_quantity_gte_1",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the line."""
        return f"Cart {self.cart_id}: variant {self.variant_id} x{self.quantity}"
