"""URL routes for the notifications app.

Mounted at ``/api/v1/`` from ``config/api_urls.py``.  The test-send
endpoint and log-retrieval endpoints are staff-only.
"""

from django.urls import path

from apps.notifications.views import (
    FailedNotificationLogsView,
    NotificationLogListView,
    SendTestSMSView,
)

urlpatterns = [
    path(
        "notifications/send-test-sms/",
        SendTestSMSView.as_view(),
        name="notification-send-test-sms",
    ),
    path(
        "notifications/logs/",
        NotificationLogListView.as_view(),
        name="notification-log-list",
    ),
    path(
        "notifications/logs/failed/",
        FailedNotificationLogsView.as_view(),
        name="notification-log-failed",
    ),
]
