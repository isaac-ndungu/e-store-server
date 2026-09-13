"""URL routes for the cart app."""

from django.urls import path

from apps.cart.views import CartDetailView, CartItemCreateView, CartItemDetailView

urlpatterns = [
    path("cart/", CartDetailView.as_view(), name="cart-detail"),
    path("cart/items/", CartItemCreateView.as_view(), name="cart-item-add"),
    path(
        "cart/items/<int:item_id>/",
        CartItemDetailView.as_view(),
        name="cart-item-detail",
    ),
]
