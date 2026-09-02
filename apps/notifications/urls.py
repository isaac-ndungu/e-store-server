"""URL routes for the notifications app.

Mounted at ``/api/v1/`` from ``config/api_urls.py``.  The test-send
endpoint and log-retrieval endpoints are staff-only.  The delivery-report
endpoint is public because Africa's Talking (the SMS provider) calls it
with delivery callbacks, not the user.
"""

from django.urls import path

from apps.notifications.views import (
    DeliveryReportView,
    FailedNotificationLogsView,
    NotificationLogDetailView,
    NotificationLogListView,
    SendTestSMSView,
)

urlpatterns = [
    path(
        "notifications/send-test-sms/",
        SendTestSMSView.as_view(),
        name="notification-send-test-sms",
    ),
    # Detail routes are registered before list routes so "logs/<int:pk>/"
    # and "logs/<str:sub>/" style prefixes never capture the literal
    # "failed" segment as an id.
    path(
        "notifications/logs/<int:pk>/",
        NotificationLogDetailView.as_view(),
        name="notification-log-detail",
    ),
    path(
        "notifications/logs/failed/",
        FailedNotificationLogsView.as_view(),
        name="notification-log-failed",
    ),
    path(
        "notifications/logs/",
        NotificationLogListView.as_view(),
        name="notification-log-list",
    ),
    path(
        "notifications/delivery-reports/",
        DeliveryReportView.as_view(),
        name="notification-delivery-report",
    ),
]
