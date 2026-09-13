from django.urls import path

from apps.orders.views import OrderStatusUpdateView, StaffOrderIntakeView

urlpatterns = [
    path("orders/intake/", StaffOrderIntakeView.as_view(), name="order-intake"),
    path(
        "orders/<int:order_id>/status/",
        OrderStatusUpdateView.as_view(),
        name="order-status-update",
    ),
]
