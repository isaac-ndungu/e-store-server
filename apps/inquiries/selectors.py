"""Read-only query helpers for the inquiries app."""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.inquiries.models import Inquiry, normalize_short_reference


def list_inquiries(
    *,
    status=None,
    channel=None,
    email_status=None,
    search=None,
    no_message_after_1h=False,
    has_phone=False,
    product=None,
):
    """Return inquiry rows for the staff queue, newest first.

    Args:
        status (str | None): optional status filter.
        channel (str | None): optional channel filter.
        email_status (str | None): optional email delivery filter. ``problem``
            selects email rows that are pending or failed.
        search (str | None): optional free text matching a reference code
            (``VF-4K8P``, ``INQ-000123``, or a bare number) or contact substring.
        no_message_after_1h (bool): when True, select WhatsApp clicks older
            than 1 hour that are still in ``clicked`` status.
        has_phone (bool): when True, select rows with a non-empty phone number.
        product (str | None): optional SKU or product name substring to match
            against ``cart_snapshot``.

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
    if email_status == "problem":
        queryset = queryset.filter(
            channel="email", email_status__in=["pending", "failed"]
        )
    elif email_status is not None:
        queryset = queryset.filter(email_status=email_status)
    if no_message_after_1h:
        cutoff = timezone.now() - timedelta(hours=1)
        queryset = queryset.filter(
            channel="whatsapp",
            status="clicked",
            created_at__lte=cutoff,
        )
    if has_phone:
        queryset = queryset.exclude(customer_phone="")
    if product:
        product_term = product.strip()
        if product_term:
            matching_ids = [
                row.pk
                for row in queryset.only("pk", "cart_snapshot")
                if any(
                    product_term.lower() in str(line.get("sku", "")).lower()
                    or product_term.lower() in str(line.get("name", "")).lower()
                    for line in (row.cart_snapshot or [])
                    if isinstance(line, dict)
                )
            ]
            queryset = queryset.filter(pk__in=matching_ids)
    if search:
        lookup = search.strip()
        short_ref = normalize_short_reference(lookup)
        candidate_pk = Inquiry.parse_reference(lookup)
        contact_match = (
            Q(reference_code__icontains=lookup)
            | Q(contact_hint__icontains=lookup)
            | Q(customer_name__icontains=lookup)
            | Q(customer_phone__icontains=lookup)
            | Q(customer_email__icontains=lookup)
            | Q(delivery_location__icontains=lookup)
        )
        ref_match = Q()
        if short_ref is not None:
            ref_match |= Q(reference_code__iexact=short_ref)
        if candidate_pk is not None:
            ref_match |= Q(pk=candidate_pk)
        queryset = queryset.filter(ref_match | contact_match)
    return queryset
