"""Read-only query helpers for the inquiries app."""

from apps.inquiries.models import Inquiry


def list_inquiries(*, status=None, channel=None):
    """Return inquiry rows for the staff queue, newest first.

    Args:
        status (str | None): optional status filter.
        channel (str | None): optional channel filter.

    Returns:
        QuerySet: inquiries with the converted order pre-fetched.
    """
    queryset = Inquiry.objects.select_related("converted_order").order_by(
        "-created_at", "-pk"
    )
    if status is not None:
        queryset = queryset.filter(status=status)
    if channel is not None:
        queryset = queryset.filter(channel=channel)
    return queryset
