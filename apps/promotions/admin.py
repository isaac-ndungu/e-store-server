from django import forms
from django.contrib import admin

from apps.promotions.models import Coupon, CouponRedemption, Discount

_SCOPE_RELATION = {
    "variant": "variants",
    "product": "products",
    "category": "categories",
    "brand": "brands",
}


class DiscountAdminForm(forms.ModelForm):
    """Admin form that enforces the scope-relation pairing.

    ``Discount.clean()`` skips its many-to-many check on an unsaved instance
    (the relations are populated only after save), so this form validates the
    pairing from the submitted relation selections up front.
    """

    class Meta:
        model = Discount
        fields = "__all__"

    def clean(self):
        """Require a matching relation for non-sitewide, non-bundle scopes.

        Returns:
            dict: the cleaned data.

        Raises:
            ValidationError: if the scope is empty of a matching relation.
        """
        cleaned = super().clean()
        scope = cleaned.get("scope")
        if scope in _SCOPE_RELATION:
            relation_field = _SCOPE_RELATION[scope]
            if not cleaned.get(relation_field):
                raise forms.ValidationError(
                    {relation_field: (f"Select at least one {scope} for this scope.")}
                )
        return cleaned


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    """Admin page for discounts."""

    form = DiscountAdminForm
    list_display = (
        "name",
        "scope",
        "discount_type",
        "value",
        "priority",
        "is_active",
        "starts_at",
        "ends_at",
        "applies_within_bundles",
        "redemption_count",
    )
    list_filter = ("scope", "discount_type", "is_active", "applies_within_bundles")
    search_fields = ("name", "badge_text")
    filter_horizontal = ("variants", "products", "categories", "brands")


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    """Admin page for coupons."""

    list_display = (
        "code",
        "discount_type",
        "value",
        "min_order_value",
        "is_active",
        "starts_at",
        "ends_at",
        "usage_limit_total",
        "usage_limit_per_user",
    )
    list_filter = ("discount_type", "is_active", "stackable_with_discounts")
    search_fields = ("code",)
    filter_horizontal = ("applies_to_products", "applies_to_categories")


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    """Read-only admin page for coupon redemptions."""

    list_display = ("coupon", "user", "redeemed_at")
    list_filter = ("coupon", "redeemed_at")
    search_fields = ("coupon__code",)
    readonly_fields = ("coupon", "user", "redeemed_at")
