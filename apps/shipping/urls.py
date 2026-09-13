"""URL routing for the public and admin shipping endpoints."""

from django.urls import path

from apps.shipping.views import (
    AdminDeliveryAreaDetailView,
    AdminDeliveryAreaListCreateView,
    DeliveryAreaListView,
)

urlpatterns = [
    path(
        "shipping/delivery-areas/",
        DeliveryAreaListView.as_view(),
        name="delivery-areas",
    ),
    # Admin CRUD
    path(
        "shipping/admin/delivery-areas/",
        AdminDeliveryAreaListCreateView.as_view(),
        name="admin-delivery-area-list-create",
    ),
    path(
        "shipping/admin/delivery-areas/<int:pk>/",
        AdminDeliveryAreaDetailView.as_view(),
        name="admin-delivery-area-detail",
    ),
]
