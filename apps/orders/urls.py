from django.urls import path

from apps.orders.views import (
    OrderDetailView,
    OrderListCreateView,
    OrderResendOTPView,
    OrderStatusHistoryView,
    OrderVerifyOTPView,
)

urlpatterns = [
    path("orders/", OrderListCreateView.as_view(), name="order-list"),
    path("orders/<int:order_id>/", OrderDetailView.as_view(), name="order-detail"),
    path(
        "orders/<int:order_id>/verify-otp/",
        OrderVerifyOTPView.as_view(),
        name="order-verify-otp",
    ),
    path(
        "orders/<int:order_id>/resend-otp/",
        OrderResendOTPView.as_view(),
        name="order-otp-resend",
    ),
    path(
        "orders/<int:order_id>/status-history/",
        OrderStatusHistoryView.as_view(),
        name="order-status-history",
    ),
]
