"""URL routing for the public and admin shipping endpoints."""

from django.urls import path

from apps.shipping.views import (
    AdminDeliveryZoneDetailView,
    AdminDeliveryZoneListCreateView,
    AdminZonePriorityDetailView,
    AdminZonePriorityListCreateView,
    DeliveryZoneListView,
    ShippingQuoteView,
)

urlpatterns = [
    path(
        "shipping/delivery-zones/",
        DeliveryZoneListView.as_view(),
        name="delivery-zones",
    ),
    path("shipping/quote/", ShippingQuoteView.as_view(), name="quote"),
    # Admin CRUD
    path(
        "shipping/admin/delivery-zones/",
        AdminDeliveryZoneListCreateView.as_view(),
        name="admin-delivery-zone-list-create",
    ),
    path(
        "shipping/admin/delivery-zones/<int:pk>/",
        AdminDeliveryZoneDetailView.as_view(),
        name="admin-delivery-zone-detail",
    ),
    path(
        "shipping/admin/zone-priorities/",
        AdminZonePriorityListCreateView.as_view(),
        name="admin-zone-priority-list-create",
    ),
    path(
        "shipping/admin/zone-priorities/<int:pk>/",
        AdminZonePriorityDetailView.as_view(),
        name="admin-zone-priority-detail",
    ),
]
