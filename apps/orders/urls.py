from django.urls import path

from apps.orders.views import (
    OrderCancelView,
    OrderDetailView,
    OrderListCreateView,
    OrderResendOTPView,
    OrderStatusHistoryView,
    OrderStatusUpdateView,
    OrderVerifyOTPView,
)

urlpatterns = [
    path("orders/", OrderListCreateView.as_view(), name="order-list"),
    # The order reference is either an integer id (authenticated caller) or an
    # unguessable lookup token (guest caller); the views resolve either form.
    path("orders/<str:order_ref>/", OrderDetailView.as_view(), name="order-detail"),
    path(
        "orders/<str:order_ref>/cancel/",
        OrderCancelView.as_view(),
        name="order-cancel",
    ),
    path(
        "orders/<int:order_id>/status/",
        OrderStatusUpdateView.as_view(),
        name="order-status-update",
    ),
    path(
        "orders/<str:order_ref>/verify-otp/",
        OrderVerifyOTPView.as_view(),
        name="order-verify-otp",
    ),
    path(
        "orders/<str:order_ref>/resend-otp/",
        OrderResendOTPView.as_view(),
        name="order-otp-resend",
    ),
    path(
        "orders/<str:order_ref>/status-history/",
        OrderStatusHistoryView.as_view(),
        name="order-status-history",
    ),
]
