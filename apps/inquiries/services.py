"""Business logic for the inquiries app.

Inquiry capture is intentionally dumb: validate, store, return. No pricing,
no stock, no notifications happen here — the endpoint is fire-and-forget
from the storefront's perspective, and anything slow would punish visitors
on patchy connections. Queue movement is the only mutation, guarded by a
small allowed-transition map so an inquiry cannot skip from new to converted
without staff contact in between — except the direct new-to-abandoned spam
path.
"""

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
