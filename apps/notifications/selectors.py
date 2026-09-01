"""Selectors for the notifications app.

Read-only query helpers used by views and future callers (order status
updates, OTP dispatch, etc.) so every consumer shares the same filter
logic and queryset optimization.
"""

from apps.notifications.models import NotificationLog


def get_notification_logs_for_recipient(recipient, channel=None):
    """Return notification logs for a specific recipient, newest first.

    Optionally filters by channel (``sms``, ``email``). The queryset is
    not evaluated — callers can further slice, paginate, or aggregate it.

    Args:
        recipient (str): the phone number or email to look up.
        channel (str | None): optional channel filter.

    Returns:
        QuerySet: matching ``NotificationLog`` rows ordered by ``-created_at``.
    """
    qs = NotificationLog.objects.filter(recipient=recipient)
    if channel:
        qs = qs.filter(channel=channel)
    return qs.select_related("sent_by")


def get_failed_notification_logs():
    """Return all notification logs with a ``failed`` status, newest first.

    Useful for operational dashboards and retry logic.

    Returns:
        QuerySet: failed ``NotificationLog`` rows ordered by ``-created_at``.
    """
    return NotificationLog.objects.filter(status="failed").select_related("sent_by")


def get_notification_logs_by_purpose(purpose):
    """Return notification logs filtered by purpose, newest first.

    Args:
        purpose (str): one of the ``NotificationLog.PURPOSE_CHOICES`` values.

    Returns:
        QuerySet: matching ``NotificationLog`` rows ordered by ``-created_at``.
    """
    return NotificationLog.objects.filter(purpose=purpose).select_related("sent_by")
