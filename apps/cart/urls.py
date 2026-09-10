from django.urls import path

from apps.cart.views import (
    CartApplyCouponView,
    CartItemDetailView,
    CartItemsView,
    CartRemoveCouponView,
    CartView,
)

urlpatterns = [
    # Cart
    path("cart/", CartView.as_view(), name="cart"),
    path("cart/items/", CartItemsView.as_view(), name="cart-items"),
    path(
        "cart/items/<int:item_id>/",
        CartItemDetailView.as_view(),
        name="cart-item-detail",
    ),
    path("cart/apply-coupon/", CartApplyCouponView.as_view(), name="cart-apply-coupon"),
    path(
        "cart/remove-coupon/", CartRemoveCouponView.as_view(), name="cart-remove-coupon"
    ),
]
