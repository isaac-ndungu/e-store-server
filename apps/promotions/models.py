"""Data models for the promotions app.

Promotions change what a shopper is charged: ``Discount`` models automatic
price reductions (on variants, products, categories, brands, bundles, or
sitewide), ``Coupon`` models an optional code a shopper can apply at checkout,
and ``CouponRedemption`` records each time a coupon is used so usage limits
can be enforced.

The authoritative price — after any discount or coupon — is produced by
``apps.promotions.services.get_effective_price``, never by a figure supplied
by a client. Money fields are ``DecimalField`` throughout (never ``float``) so
discount arithmetic stays exact and ``min`` / truncation never compounds
rounding error.
"""

import decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

MIN_ZERO = MinValueValidator(decimal.Decimal("0.00"))


class Discount(models.Model):
    """An automatic price reduction applied to a set of products.

    ``scope`` decides which catalogue entities a discount covers — a specific
    set of variants, products, categories, brands, one bundle, or every
    product (sitewide). When ``scope`` is not ``sitewide``, at least one of
    the matching relation fields (``variants`` / ``products`` / ``categories``
    / ``brands`` / ``bundle``) must be populated, enforced by ``clean()``.

    ``priority`` breaks ties when several discounts match the same variant:
    the highest ``priority`` wins, then the discount that yields the lowest
    price. ``applies_within_bundles`` controls the double-discount guard — a
    bundle already has its own discount, so an item discount only stacks with
    it when this flag is set. ``max_redemptions`` / ``redemption_count`` cap
    how many times the discount may be applied in total.
    """

    SCOPE_CHOICES = (
        ("variant", "Specific Variant(s)"),
        ("product", "Specific Product(s)"),
        ("category", "Category"),
        ("brand", "Brand"),
        ("bundle", "Bundle"),
        ("sitewide", "Sitewide"),
    )
    DISCOUNT_TYPE_CHOICES = (
        ("percent", "Percentage"),
        ("fixed", "Fixed Amount"),
    )

    name = models.CharField(max_length=255)
    badge_text = models.CharField(max_length=50, blank=True)
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPE_CHOICES)
    value = models.DecimalField(max_digits=10, decimal_places=2, validators=[MIN_ZERO])
    variants = models.ManyToManyField(
        "catalog.ProductVariant", blank=True, related_name="discounts"
    )
    products = models.ManyToManyField(
        "catalog.Product", blank=True, related_name="discounts"
    )
    categories = models.ManyToManyField(
        "catalog.Category", blank=True, related_name="discounts"
    )
    brands = models.ManyToManyField(
        "catalog.Brand", blank=True, related_name="discounts"
    )
    bundle = models.ForeignKey(
        "bundles.Bundle",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="discounts",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=0)
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    redemption_count = models.PositiveIntegerField(default=0)
    applies_within_bundles = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "-created_at"]
        indexes = [
            models.Index(fields=["scope"], name="disc_scope_idx"),
            models.Index(fields=["is_active"], name="disc_active_idx"),
            models.Index(fields=["starts_at"], name="disc_start_idx"),
            models.Index(fields=["ends_at"], name="disc_end_idx"),
        ]

    def __str__(self):
        """Return the discount name and scope."""
        return f"{self.name} ({self.get_scope_display()})"

    def clean(self):
        """Validate window bounds and scope-specific relations.

        Raises:
            ValidationError: if the window is reversed or a non-sitewide scope
                has no matching relation populated.
        """
        super().clean()
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValidationError({"ends_at": "ends_at must be on or after starts_at."})
        if (
            self.discount_type == "percent"
            and self.value is not None
            and self.value > 100
        ):
            raise ValidationError(
                {"value": "A percentage discount cannot exceed 100%."}
            )
        if self.scope == "bundle" and not self.bundle_id:
            raise ValidationError(
                {"bundle": "A bundle-scoped discount must select a bundle."}
            )
        relation_fields = {
            "variant": ("variants",),
            "product": ("products",),
            "category": ("categories",),
            "brand": ("brands",),
        }
        if self.scope in relation_fields and self.pk is not None:
            field_name = relation_fields[self.scope][0]
            if getattr(self, field_name).count() == 0:
                raise ValidationError(
                    {
                        field_name: (
                            f"At least one {self.get_scope_display()} must be "
                            "selected for this scope."
                        )
                    }
                )


class Coupon(models.Model):
    """A code a shopper can apply at checkout for a discount.

    ``discount_type`` is ``percent`` (``value`` is a percentage), ``fixed``
    (``value`` is a flat amount), or ``free_shipping`` (no ``value`` needed).
    A coupon is valid only while ``is_active`` and inside its window, and
    when it still has usage budget: a global ``usage_limit_total`` and a per
    user ``usage_limit_per_user``. ``min_order_value`` and the
    ``applies_to_products`` / ``applies_to_categories`` relations restrict
    which carts it may be used against — evaluated at checkout where cart
    totals exist. ``stackable_with_discounts`` decides whether a coupon
    applies on top of an already discounted price.
    """

    DISCOUNT_TYPE_CHOICES = (
        ("percent", "Percentage"),
        ("fixed", "Fixed Amount"),
        ("free_shipping", "Free Shipping"),
    )

    code = models.CharField(max_length=50, unique=True)
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES)
    value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MIN_ZERO],
    )
    min_order_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    applies_to_products = models.ManyToManyField(
        "catalog.Product", blank=True, related_name="coupons"
    )
    applies_to_categories = models.ManyToManyField(
        "catalog.Category", blank=True, related_name="coupons"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    usage_limit_total = models.PositiveIntegerField(null=True, blank=True)
    usage_limit_per_user = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    stackable_with_discounts = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["code"], name="coup_code_idx"),
            models.Index(fields=["is_active"], name="coup_active_idx"),
            models.Index(fields=["starts_at"], name="coup_start_idx"),
            models.Index(fields=["ends_at"], name="coup_end_idx"),
        ]

    def __str__(self):
        """Return the coupon code."""
        return self.code

    def clean(self):
        """Validate window bounds and that a money coupon has a value.

        Raises:
            ValidationError: if the window is reversed or a percent/fixed
                coupon has no ``value``.
        """
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()
        if self.discount_type != "free_shipping" and self.value is None:
            raise ValidationError(
                {"value": "value is required for percent and fixed coupons."}
            )
        if (
            self.discount_type == "percent"
            and self.value is not None
            and self.value > 100
        ):
            raise ValidationError({"value": "A percentage coupon cannot exceed 100%."})
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValidationError({"ends_at": "ends_at must be on or after starts_at."})
        duplicates = Coupon.objects.filter(code__iexact=self.code)
        if self.pk is not None:
            duplicates = duplicates.exclude(pk=self.pk)
        if duplicates.exists():
            raise ValidationError({"code": "A coupon with this code already exists."})


class CouponRedemption(models.Model):
    """A record of a coupon having been used, for usage-limit accounting.

    ``user`` is nullable because a guest checkout (with no persisted account)
    can still apply a coupon. ``order`` records which order the coupon was
    redeemed against; it is nullable because redemption rows predate the
    orders table's existence.
    """

    coupon = models.ForeignKey(
        Coupon, related_name="redemptions", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="coupon_redemptions",
    )
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="coupon_redemptions",
    )
    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-redeemed_at"]
        indexes = [
            models.Index(fields=["coupon"], name="red_coupon_idx"),
            models.Index(fields=["user"], name="red_user_idx"),
            models.Index(fields=["redeemed_at"], name="red_at_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying coupon and redeemer."""
        user = self.user_id or "guest"
        return f"{self.coupon_id} by {user} at {self.redeemed_at}"
