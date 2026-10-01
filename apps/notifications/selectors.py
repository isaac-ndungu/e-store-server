from apps.notifications.models import NotificationLog


def _base_queryset():
    """Return the base notification-log queryset with the sender prefetched.

    Returns:
        QuerySet: ``NotificationLog`` rows ordered by ``-created_at`` with
            ``sent_by`` eagerly loaded to avoid per-row queries.
    """
    return NotificationLog.objects.select_related("sent_by").order_by("-created_at")


def get_notification_logs(recipient=None, channel=None, status=None, purpose=None):
    """Return notification logs matching any combination of filters.

    All supplied filters are combined (ANDed) so a caller can narrow by
    recipient and purpose at once.

    Args:
        recipient (str | None): exact phone number or email match.
        channel (str | None): ``"sms"`` / ``"email"`` filter.
        status (str | None): log status filter.
        purpose (str | None): notification purpose filter.

    Returns:
        QuerySet: matching ``NotificationLog`` rows, newest first.
    """
    qs = _base_queryset()
    if recipient:
        from apps.accounts.services import mask_email, mask_phone

        masked = mask_email(recipient) if "@" in recipient else mask_phone(recipient)
        qs = qs.filter(recipient=masked)
    if channel:
        qs = qs.filter(channel=channel)
    if status:
        qs = qs.filter(status=status)
    if purpose:
        qs = qs.filter(purpose=purpose)
    return qs
