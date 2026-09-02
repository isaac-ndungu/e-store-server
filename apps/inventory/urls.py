from django.urls import path

from apps.inventory.views import (
    AdminInventoryDetailView,
    AdminInventoryListCreateView,
    AdminReservationDetailView,
    AdminReservationListView,
    AdminReservationReleaseView,
    AdminSerialUnitDetailView,
    AdminSerialUnitListCreateView,
    AdminWarehouseDetailView,
    AdminWarehouseListCreateView,
    AvailabilityView,
    BulkAvailabilityView,
)

urlpatterns = [
    path("inventory/availability/", AvailabilityView.as_view(), name="availability"),
    path(
        "inventory/availability/bulk/",
        BulkAvailabilityView.as_view(),
        name="availability-bulk",
    ),
    # Admin CRUD
    path(
        "inventory/admin/warehouses/",
        AdminWarehouseListCreateView.as_view(),
        name="admin-warehouse-list-create",
    ),
    path(
        "inventory/admin/warehouses/<int:pk>/",
        AdminWarehouseDetailView.as_view(),
        name="admin-warehouse-detail",
    ),
    path(
        "inventory/admin/inventory/",
        AdminInventoryListCreateView.as_view(),
        name="admin-inventory-list-create",
    ),
    path(
        "inventory/admin/inventory/<int:pk>/",
        AdminInventoryDetailView.as_view(),
        name="admin-inventory-detail",
    ),
    path(
        "inventory/admin/serial-units/",
        AdminSerialUnitListCreateView.as_view(),
        name="admin-serial-unit-list-create",
    ),
    path(
        "inventory/admin/serial-units/<int:pk>/",
        AdminSerialUnitDetailView.as_view(),
        name="admin-serial-unit-detail",
    ),
    path(
        "inventory/admin/reservations/",
        AdminReservationListView.as_view(),
        name="admin-reservation-list",
    ),
    path(
        "inventory/admin/reservations/<int:pk>/",
        AdminReservationDetailView.as_view(),
        name="admin-reservation-detail",
    ),
    path(
        "inventory/admin/reservations/<int:pk>/release/",
        AdminReservationReleaseView.as_view(),
        name="admin-reservation-release",
    ),
]
