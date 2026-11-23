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

from apps.inquiries.models import Inquiry

_ALLOWED_TRANSITIONS = {
    "new": {"contacted", "abandoned"},
    "contacted": {"converted", "abandoned"},
    "converted": set(),
    "abandoned": {"contacted"},
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
):
    """Store a hand-off capture row and queue the email hand-off when needed.

    Args:
        channel (str): ``whatsapp`` or ``email``.
        cart_snapshot (list): validated snapshot lines.
        contact_hint (str): optional combined contact summary.
        customer_name (str): optional visitor name.
        customer_phone (str): optional normalized mobile number.
        customer_email (str): optional reply-to email.
        delivery_location (str): optional delivery area or landmark.
        order_notes (str): optional order instructions.

    Returns:
        Inquiry: the created row.
    """
    hint = contact_hint or build_contact_hint(
        customer_name=customer_name,
        customer_phone=customer_phone,
        delivery_location=delivery_location,
        customer_email=customer_email,
        order_notes=order_notes,
    )
    inquiry = Inquiry.objects.create(
        channel=channel,
        cart_snapshot=cart_snapshot,
        contact_hint=hint,
        customer_name=customer_name or "",
        customer_phone=customer_phone or "",
        customer_email=customer_email or "",
        delivery_location=delivery_location or "",
        order_notes=order_notes or "",
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
    inquiry.save(update_fields=["status", "updated_at"])
    return inquiry


def build_handoff_message(*, cart_snapshot, reference):
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
    parts.append(f"Ref {reference}")
    return "\n".join(parts)
