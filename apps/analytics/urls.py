"""URL routing for the analytics endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Every route is gated by
the manager/analyst role at the view level; the summary and report routes live
under ``analytics/`` per the public API list.
"""

from django.urls import path

from apps.analytics.views import (
    DashboardSummaryView,
    NotificationsReportView,
    ProductPerformanceReportView,
    PromotionsReportView,
    ReturnsReportView,
    ReviewsReportView,
    SalesReportView,
    StockReportView,
    SupportReportView,
    TrafficReportView,
)

urlpatterns = [
    path("analytics/summary/", DashboardSummaryView.as_view(), name="summary"),
    path("analytics/reports/sales/", SalesReportView.as_view(), name="sales-report"),
    path("analytics/reports/stock/", StockReportView.as_view(), name="stock-report"),
    path(
        "analytics/reports/products/",
        ProductPerformanceReportView.as_view(),
        name="products-report",
    ),
    path(
        "analytics/reports/promotions/",
        PromotionsReportView.as_view(),
        name="promotions-report",
    ),
    path(
        "analytics/reports/returns/",
        ReturnsReportView.as_view(),
        name="returns-report",
    ),
    path(
        "analytics/reports/support/",
        SupportReportView.as_view(),
        name="support-report",
    ),
    path(
        "analytics/reports/reviews/",
        ReviewsReportView.as_view(),
        name="reviews-report",
    ),
    path(
        "analytics/reports/traffic/",
        TrafficReportView.as_view(),
        name="traffic-report",
    ),
    path(
        "analytics/reports/notifications/",
        NotificationsReportView.as_view(),
        name="notifications-report",
    ),
]
