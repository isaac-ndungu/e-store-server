from django.urls import path

from apps.orders.views import (
    OrderStatusUpdateView,
    StaffOrderDetailView,
    StaffOrderIntakeView,
    StaffOrderListView,
)

urlpatterns = [
    path("orders/intake/", StaffOrderIntakeView.as_view(), name="order-intake"),
    path("orders/", StaffOrderListView.as_view(), name="order-staff-list"),
    path(
        "orders/<int:order_id>/",
        StaffOrderDetailView.as_view(),
        name="order-staff-detail",
    ),
    path(
        "orders/<int:order_id>/status/",
        OrderStatusUpdateView.as_view(),
        name="order-status-update",
    ),
]
