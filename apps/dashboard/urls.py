"""URL routing for the dashboard endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Every route is gated by
the manager/analyst role at the view level; the widgets live under
``dashboard/`` per the public API list.
"""

from django.urls import path

from apps.dashboard.views import (
    AlertsDashboardView,
    BundlesDashboardView,
    CodOperationsDashboardView,
    CollectionsDashboardView,
    ProductsDashboardView,
    PromotionsDashboardView,
    ReturnsDashboardView,
    SalesDashboardView,
    StockDashboardView,
    SupportDashboardView,
    WarehouseRoutingDashboardView,
)

urlpatterns = [
    path("dashboard/sales/", SalesDashboardView.as_view(), name="sales"),
    path(
        "dashboard/cod-operations/",
        CodOperationsDashboardView.as_view(),
        name="cod-operations",
    ),
    path("dashboard/stock/", StockDashboardView.as_view(), name="stock"),
    path("dashboard/products/", ProductsDashboardView.as_view(), name="products"),
    path(
        "dashboard/collections/",
        CollectionsDashboardView.as_view(),
        name="collections",
    ),
    path("dashboard/bundles/", BundlesDashboardView.as_view(), name="bundles"),
    path(
        "dashboard/promotions/",
        PromotionsDashboardView.as_view(),
        name="promotions",
    ),
    path("dashboard/returns/", ReturnsDashboardView.as_view(), name="returns"),
    path(
        "dashboard/warehouse-routing/",
        WarehouseRoutingDashboardView.as_view(),
        name="warehouse-routing",
    ),
    path("dashboard/support/", SupportDashboardView.as_view(), name="support"),
    path("dashboard/alerts/", AlertsDashboardView.as_view(), name="alerts"),
]
