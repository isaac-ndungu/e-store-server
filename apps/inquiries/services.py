"""Business logic for the inquiries app.

Inquiry capture stores the row and queues the shop notification only when the
inquiry came through the email channel. Pricing and stock stay outside this
path so the public endpoint remains fast on patchy connections. Queue movement
is guarded by a small allowed-transition map so an inquiry cannot skip from
new to converted without staff contact in between  -  except the direct
new-to-abandoned spam path.
"""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.inquiries.models import Inquiry, normalize_short_reference

_ALLOWED_TRANSITIONS = {
    "clicked": {"message_received", "contacted", "abandoned", "spam"},
    "new": {"message_received", "contacted", "abandoned", "spam"},
    "message_received": {"contacted", "converted", "abandoned", "spam"},
    "contacted": {"converted", "abandoned", "spam"},
    "converted": set(),
    "abandoned": {"contacted", "spam"},
    "spam": {"clicked", "contacted"},
}


def build_contact_hint(
    *,
    customer_name="",
    customer_phone="",
    delivery_location="",
    customer_email="",
    order_notes="",
):
    """Combine the structured contact facts into the legacy display line.

    Args:
        customer_name (str): visitor name.
        customer_phone (str): normalized mobile number.
        delivery_location (str): area or landmark.
        customer_email (str): reply-to email.
        order_notes (str): optional instructions.

    Returns:
        str: pipe-separated contact summary, blank when every part is blank.
    """
    parts = [
        (customer_name or "").strip(),
        (customer_phone or "").strip(),
        (delivery_location or "").strip(),
        (customer_email or "").strip(),
        (order_notes or "").strip(),
    ]
    return " | ".join(part for part in parts if part)[:255]


def record_inquiry(
    *,
    channel,
    cart_snapshot,
    contact_hint="",
    customer_name="",
    customer_phone="",
    customer_email="",
    delivery_location="",
    order_notes="",
    idempotency_key="",
    reference_code="",
    page_url="",
):
    """Store a hand-off capture row and queue the email hand-off when needed.

    When ``idempotency_key`` matches an existing inquiry, returns that row
    instead of creating a duplicate.

    Args:
        channel (str): ``whatsapp`` or ``email``.
        cart_snapshot (list): validated snapshot lines.
        contact_hint (str): optional combined contact summary.
        customer_name (str): optional visitor name.
        customer_phone (str): optional normalized mobile number.
        customer_email (str): optional reply-to email.
        delivery_location (str): optional delivery area or landmark.
        order_notes (str): optional order instructions.
        idempotency_key (str): optional client-generated tap key.
        reference_code (str): optional client-generated ``VF-XXXX`` candidate.
        page_url (str): optional storefront page URL where the tap occurred.

    Returns:
        Inquiry: the created or deduplicated row.
    """
    clean_key = (idempotency_key or "").strip()[:128]
    if clean_key:
        existing = Inquiry.objects.filter(idempotency_key=clean_key).first()
        if existing is not None:
            return existing

    normalized_ref = normalize_short_reference(reference_code)
    if (
        normalized_ref
        and Inquiry.objects.filter(reference_code=normalized_ref).exists()
    ):
        normalized_ref = None

    hint = contact_hint or build_contact_hint(
        customer_name=customer_name,
        customer_phone=customer_phone,
        delivery_location=delivery_location,
        customer_email=customer_email,
        order_notes=order_notes,
    )
    initial_status = "clicked" if channel == "whatsapp" else "new"
    try:
        inquiry = Inquiry.objects.create(
            channel=channel,
            reference_code=normalized_ref,
            idempotency_key=clean_key,
            page_url=(page_url or "")[:500],
            cart_snapshot=cart_snapshot,
            contact_hint=hint,
            customer_name=customer_name or "",
            customer_phone=customer_phone or "",
            customer_email=customer_email or "",
            delivery_location=delivery_location or "",
            order_notes=order_notes or "",
            status=initial_status,
            email_status="pending" if channel == "email" else "not_required",
        )
    except IntegrityError:
        if clean_key:
            existing = Inquiry.objects.filter(idempotency_key=clean_key).first()
            if existing is not None:
                return existing
        inquiry = Inquiry.objects.create(
            channel=channel,
            idempotency_key=clean_key,
            page_url=(page_url or "")[:500],
            cart_snapshot=cart_snapshot,
            contact_hint=hint,
            customer_name=customer_name or "",
            customer_phone=customer_phone or "",
            customer_email=customer_email or "",
            delivery_location=delivery_location or "",
            order_notes=order_notes or "",
            status=initial_status,
            email_status="pending" if channel == "email" else "not_required",
        )
    if inquiry.channel == "email":
        from apps.inquiries.email import queue_order_intake_email

        queue_order_intake_email(inquiry)
    return inquiry


def transition_inquiry(inquiry, to_status):
    """Move an inquiry to a new queue status.

    Args:
        inquiry (Inquiry): the row to move.
        to_status (str): the target status.

    Returns:
        Inquiry: the updated row.

    Raises:
        ValidationError: if the transition is not allowed.
    """
    allowed = _ALLOWED_TRANSITIONS.get(inquiry.status, set())
    if to_status not in allowed:
        raise ValidationError(
            f"Cannot move inquiry from '{inquiry.status}' to '{to_status}'."
        )
    inquiry.status = to_status
    update_fields = ["status", "updated_at"]
    if to_status == "message_received" and inquiry.message_received_at is None:
        inquiry.message_received_at = timezone.now()
        update_fields.append("message_received_at")
    inquiry.save(update_fields=update_fields)
    return inquiry


def find_inquiry_by_reference(reference_input):
    """Locate an inquiry by short reference code (``VF-XXXX``) or legacy pk.

    Args:
        reference_input (str): staff-entered or scanned reference.

    Returns:
        Inquiry | None: the matching inquiry row, or ``None``.
    """
    short_code = normalize_short_reference(reference_input)
    if short_code is not None:
        match = Inquiry.objects.filter(reference_code__iexact=short_code).first()
        if match is not None:
            return match
    candidate_pk = Inquiry.parse_reference(reference_input)
    if candidate_pk is not None:
        return Inquiry.objects.filter(pk=candidate_pk).first()
    return None


def mark_inquiry_message_received(reference_input):
    """Mark an inquiry as ``message_received`` from its reference code.

    Idempotent when the inquiry is already in ``message_received`` status.

    Args:
        reference_input (str): short code (``VF-XXXX``) or legacy reference.

    Returns:
        Inquiry: the updated inquiry.

    Raises:
        Inquiry.DoesNotExist: when no inquiry matches the reference.
        ValidationError: when the inquiry cannot transition to ``message_received``.
    """
    inquiry = find_inquiry_by_reference(reference_input)
    if inquiry is None:
        raise Inquiry.DoesNotExist("No inquiry matches that reference code.")
    if inquiry.status == "message_received":
        return inquiry
    return transition_inquiry(inquiry, "message_received")


def update_inquiry_recovery_contact(inquiry, *, customer_name="", customer_phone=""):
    """Attach optional recovery contact details to an existing inquiry.

    Only non-empty values overwrite existing fields so submitting a phone
    alone never erases a previously supplied name.

    Args:
        inquiry (Inquiry): the inquiry to update.
        customer_name (str): optional sanitized visitor name.
        customer_phone (str): optional normalized ``+254`` mobile number.

    Returns:
        Inquiry: the updated inquiry.
    """
    update_fields = ["updated_at"]
    if customer_name:
        inquiry.customer_name = customer_name
        update_fields.append("customer_name")
    if customer_phone:
        inquiry.customer_phone = customer_phone
        update_fields.append("customer_phone")
    hint = build_contact_hint(
        customer_name=inquiry.customer_name,
        customer_phone=inquiry.customer_phone,
        delivery_location=inquiry.delivery_location,
        customer_email=inquiry.customer_email,
        order_notes=inquiry.order_notes,
    )
    if hint != inquiry.contact_hint:
        inquiry.contact_hint = hint
        update_fields.append("contact_hint")
    if len(update_fields) > 1:
        inquiry.save(update_fields=update_fields)
    return inquiry


def bulk_mark_inquiries_spam(inquiry_ids):
    """Mark selected non-converted inquiries as ``spam``.

    Args:
        inquiry_ids (list[int]): inquiry primary keys to mark as spam.

    Returns:
        dict: ``updated_count`` and ``skipped_ids`` (converted or missing rows).
    """
    unique_ids = list(dict.fromkeys(inquiry_ids))
    with transaction.atomic():
        rows = list(Inquiry.objects.select_for_update().filter(pk__in=unique_ids))
        found_ids = {row.pk for row in rows}
        skipped_ids = [pk for pk in unique_ids if pk not in found_ids]
        updatable_ids = []
        for row in rows:
            if row.status == "converted":
                skipped_ids.append(row.pk)
            elif row.status != "spam":
                updatable_ids.append(row.pk)
        updated_count = 0
        if updatable_ids:
            updated_count = Inquiry.objects.filter(pk__in=updatable_ids).update(
                status="spam", updated_at=timezone.now()
            )
    return {
        "updated_count": updated_count,
        "skipped_ids": skipped_ids,
    }


def build_handoff_message(*, cart_snapshot, reference, page_url=""):
    """Build the canonical pre-filled WhatsApp/email message text.

    The storefront URL-encodes this into the ``wa.me``/``mailto:`` link
    instead of assembling the text itself, so the ``Ref`` line matching the
    chat back to its queue row is always present and always formatted the
    same way. Prices come from the display-time snapshot and are labelled
    estimates  -  the intake view reprices everything server-side.

    Args:
        cart_snapshot (list): validated snapshot lines with ``sku``,
            ``name``, ``quantity``, and optional ``price``.
        reference (str): the inquiry reference code (``INQ-000123``).

    Returns:
        str: plain-text message (no markup or emoji, for safe encoding).
    """
    item_lines = []
    total = Decimal("0")
    total_known = True
    for line in cart_snapshot or []:
        sku = line.get("sku", "")
        name = line.get("name", "")
        try:
            quantity = int(line.get("quantity", 1))
        except TypeError, ValueError:
            quantity = 1
        try:
            unit_price = Decimal(str(line.get("price", "")))
            line_total = unit_price * quantity
            total += line_total
            price_part = f"  -  KES {line_total:,.2f}"
        except InvalidOperation, ValueError, TypeError:
            total_known = False
            price_part = "  -  price to confirm"
        item_lines.append(f"- {name} ({sku}) x{quantity}{price_part}")
    parts = ["Hello! I would like to place an order:", ""]
    parts.extend(item_lines)
    parts.append("")
    if total_known and item_lines:
        parts.append(f"Estimated total: KES {total:,.2f} (shipping to be confirmed)")
        parts.append("")
    if page_url:
        parts.append(page_url)
    parts.append(f"Ref {reference}")
    return "\n".join(parts)
