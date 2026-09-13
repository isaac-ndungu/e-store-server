"""Business logic for the inquiries app.

Inquiry capture is intentionally dumb: validate, store, return. No pricing,
no stock, no notifications happen here — the endpoint is fire-and-forget
from the storefront's perspective, and anything slow would punish visitors
on patchy connections. Queue movement is the only mutation, guarded by a
small allowed-transition map so an inquiry cannot skip from new to converted
without staff contact in between — except the direct new-to-abandoned spam
path.
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


def record_inquiry(*, channel, cart_snapshot, contact_hint=""):
    """Store a hand-off capture row.

    Args:
        channel (str): ``whatsapp`` or ``email``.
        cart_snapshot (list): validated snapshot lines.
        contact_hint (str): optional visitor-supplied phone/email.

    Returns:
        Inquiry: the created row.
    """
    return Inquiry.objects.create(
        channel=channel,
        cart_snapshot=cart_snapshot,
        contact_hint=contact_hint or "",
    )


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
    estimates — the intake view reprices everything server-side.

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
            price_part = f" — KES {line_total:,.2f}"
        except InvalidOperation, ValueError, TypeError:
            total_known = False
            price_part = " — price to confirm"
        item_lines.append(f"- {name} ({sku}) x{quantity}{price_part}")
    parts = ["Hello! I would like to place an order:", ""]
    parts.extend(item_lines)
    parts.append("")
    if total_known and item_lines:
        parts.append(f"Estimated total: KES {total:,.2f} (shipping to be confirmed)")
        parts.append("")
    parts.append(f"Ref {reference}")
    return "\n".join(parts)
