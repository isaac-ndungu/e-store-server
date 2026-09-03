from django.contrib import admin

from apps.cart.models import Cart, CartItem, WishlistItem


class CartItemInline(admin.TabularInline):
    """Inline display of cart items on the Cart detail page."""

    model = CartItem
    extra = 0
    readonly_fields = ("added_at",)


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    """Admin configuration for Cart."""

    list_display = ("id", "user", "session_key", "coupon", "updated_at")
    list_filter = ("coupon",)
    search_fields = ("user__email", "session_key")
    inlines = [CartItemInline]
    readonly_fields = ("created_at", "updated_at")


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    """Admin configuration for CartItem."""

    list_display = ("id", "cart", "variant", "bundle", "quantity", "added_at")
    list_filter = ("cart",)
    readonly_fields = ("added_at",)


@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    """Admin configuration for WishlistItem."""

    list_display = ("id", "user", "product", "added_at")
    search_fields = ("user__email", "product__name")
    readonly_fields = ("added_at",)
