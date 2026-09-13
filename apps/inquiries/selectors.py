"""Read-only query helpers for the inquiries app."""

from django.db.models import Q

from apps.inquiries.models import Inquiry


def list_inquiries(*, status=None, channel=None, search=None):
    """Return inquiry rows for the staff queue, newest first.

    Args:
        status (str | None): optional status filter.
        channel (str | None): optional channel filter.
        search (str | None): optional free text matching a reference code
            (``INQ-000123`` or a bare number) or a contact-hint substring,
            so staff can jump from an incoming chat to its queue row.

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
    if search:
        lookup = search.strip()
        candidate_pk = Inquiry.parse_reference(lookup)
        if candidate_pk is not None:
            queryset = queryset.filter(
                Q(pk=candidate_pk) | Q(contact_hint__icontains=lookup)
            )
        else:
            queryset = queryset.filter(Q(contact_hint__icontains=lookup))
    return queryset
