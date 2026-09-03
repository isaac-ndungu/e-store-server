"""Data models for the cart app.

A ``Cart`` groups ``CartItem`` rows and an optional ``Coupon`` reference.  A
cart belongs either to an authenticated ``User`` (via ``user`` FK) or to an
anonymous browser session (via ``session_key``), never both at the same time —
a ``CHECK`` constraint enforces this at the database level.

Each ``CartItem`` holds either a product-variant reference or a bundle
reference (enforced by a ``CHECK`` constraint), a quantity, and the
server-computed unit price snapshot.  The price is always recomputed from the
promotions service at read time — the stored snapshot is for order-history
reconstruction when the item becomes an ``OrderItem``, not for charging.

``WishlistItem`` is a per-user list of products they are interested in,
independent of the cart.
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class Cart(models.Model):
    """A shopping cart belonging to a user or an anonymous session.

    Exactly one of ``user`` or ``session_key`` must be set.  A logged-in
    user's cart is looked up by ``user``; a guest cart is identified by the
    session key issued by Django's session middleware.  The ``coupon``
    relation is optional and resolved at checkout for final pricing — the
    cart stores the reference so the coupon can be validated and the discount
    previewed before the order is placed.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="carts",
    )
    session_key = models.CharField(max_length=100, blank=True, db_index=True)
    coupon = models.ForeignKey(
        "promotions.Coupon",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="carts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    abandoned_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user"], name="cart_user_idx"),
            models.Index(fields=["session_key"], name="cart_session_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(user__isnull=False) | models.Q(session_key__gt="")),
                name="cart_user_or_session_required",
            ),
            models.CheckConstraint(
                condition=(models.Q(user__isnull=True) | models.Q(session_key="")),
                name="cart_not_both_user_and_session",
            ),
        ]

    def __str__(self):
        """Return a short label identifying the cart owner."""
        if self.user_id:
            return f"Cart(user={self.user_id})"
        return f"Cart(session={self.session_key})"


class CartItem(models.Model):
    """A single line in a shopping cart.

    Exactly one of ``variant`` or ``bundle`` must be set — a cart line is
    either a product variant or a bundle, never both and never neither.

    ``quantity`` is always at least 1; removing an item deletes the row
    entirely rather than setting quantity to 0.
    """

    cart = models.ForeignKey(Cart, related_name="items", on_delete=models.CASCADE)
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cart_items",
    )
    bundle = models.ForeignKey(
        "bundles.Bundle",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cart_items",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["cart"], name="cartitem_cart_idx"),
            models.Index(fields=["variant"], name="cartitem_variant_idx"),
            models.Index(fields=["bundle"], name="cartitem_bundle_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(variant__isnull=False) & models.Q(bundle__isnull=True))
                    | (models.Q(variant__isnull=True) & models.Q(bundle__isnull=False))
                ),
                name="cartitem_exactly_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="cartitem_quantity_gte_1",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the cart line."""
        target = (
            f"variant={self.variant_id}"
            if self.variant_id
            else f"bundle={self.bundle_id}"
        )
        return f"CartItem({target}, qty={self.quantity})"


class WishlistItem(models.Model):
    """A product a user has added to their wishlist.

    ``user`` is required — the wishlist is always tied to an account.  The
    ``unique_together`` constraint prevents duplicate entries.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="wishlist_items",
        on_delete=models.CASCADE,
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="wishlist_items",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]
        unique_together = ("user", "product")
        indexes = [
            models.Index(fields=["user"], name="wish_user_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the wishlist entry."""
        return f"Wishlist(user={self.user_id}, product={self.product_id})"
