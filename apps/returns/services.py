"""Business logic for the returns app.

The returns service is the only layer that creates or mutates ``ReturnRequest``
rows and the only place the post-delivery return and pre-shipment cancellation
workflows are implemented:

- ``create_return_request`` — a customer opens a request against a delivered
  order within the cooling-off window.
- ``approve_return_request`` — staff fixes how the money goes back (a plain
  description, e.g. "M-Pesa - sent manually") and the server-computed
  amounts for a refund-resolution request.
- ``record_item_received`` — physical receipt of the returned goods.
- ``refund_return_request`` — staff records the manual refund: the request is
  marked ``refunded`` and the order carries the refund record.
- ``cancel_confirmed_order`` — the pre-shipment cancellation path: a required
  staff note plus the manual-refund record, then the order is cancelled.

Refunds are arranged by staff outside the system, so money never moves here —
these functions record what happened, with the amounts still computed and
capped server-side from the order snapshots.

Invariants upheld here:

- ``ReturnRequest.status`` changes only through ``transition_return_status``,
  which writes a ``ReturnRequestStatusHistory`` row; ``Order.status`` changes
  only through the orders ``transition_order`` helper, which writes the order
  audit trail.
- Refund amounts are recomputed server-side from the order snapshots and
  capped at the order's grand total; a client-supplied amount is never
  recorded as-is, and a restocking fee can never exceed the line total.
- The refund and cancellation paths lock their parent rows
  (``select_for_update``) so a duplicated or concurrent staff action can
  never record two refunds.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import SiteConfig
from apps.orders.models import Order
from apps.orders.services import transition_order
from apps.returns.models import ReturnRequest, ReturnRequestStatusHistory

logger = logging.getLogger(__name__)

_PENNY = Decimal("0.01")

# Allowed return-request status transitions. Any status change not listed here
# is rejected by ``transition_return_status``.
_RETURN_STATUS_TRANSITIONS = {
    "requested": {"approved", "rejected", "closed"},
    "approved": {"item_received", "rejected", "closed"},
    "item_received": {"refunded", "replaced", "closed"},
    "rejected": set(),
    "refunded": {"closed"},
    "replaced": {"closed"},
    "closed": set(),
}

# Money-moving terminal states, set on ``resolved_at`` by the transition.
_TERMINAL_STATUSES = {"rejected", "refunded", "replaced", "closed"}


def _require_credit_note_if_transmitted(order):
    """Enforce the credit-note rule for a reversal once invoicing exists.

    When electronic invoicing lands, reversing a transmitted invoice requires
    a credit note rather than only a status change. Until then there is no
    invoice store, so this is a no-op — but every money-moving reversal
    (post-delivery refund, pre-shipment cancellation) must call it first so
    the check cannot be bypassed later by adding a new path that forgets it.

    Args:
        order (Order): the order being reversed.

    Raises:
        ValidationError: if the order has a transmitted invoice with no
            credit note covering it.
    """
    from django.apps import apps

    if not apps.is_installed("apps.tax_compliance"):
        return
    invoice_model = apps.get_model("tax_compliance", "ETIMSInvoice")
    credit_model = apps.get_model("tax_compliance", "ETIMSCreditNote")
    invoice = invoice_model.objects.filter(order=order).first()
    if invoice is None or getattr(invoice, "status", "") != "transmitted":
        return
    covered = credit_model.objects.filter(original_invoice=invoice).exists()
    if not covered:
        raise ValidationError(
            "A transmitted invoice requires a credit note before this "
            "reversal can proceed."
        )


def _money(value):
    """Return ``value`` as an exact ``Decimal``.

    Args:
        value: a money value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def transition_return_status(return_request, to_status, *, changed_by=None, note=""):
    """Change a return request's status and log the transition.

    The status field may only change through this function — a direct write in
    a view is a bug. The transition is validated against the allowed graph and
    a ``ReturnRequestStatusHistory`` row is written in the same transaction as
    the status field, so the audit trail can never drift from the column. A
    transition into a terminal state stamps ``resolved_at``.

    Args:
        return_request (ReturnRequest): the request to transition.
        to_status (str): the target status.
        changed_by (User | None): the user performing the change, if any.
        note (str): a free-text explanation for the audit trail.

    Returns:
        ReturnRequest: the updated request.

    Raises:
        ValidationError: if the transition is not in the allowed graph.
    """
    allowed = _RETURN_STATUS_TRANSITIONS.get(return_request.status, set())
    if to_status not in allowed:
        raise ValidationError(
            f"Cannot transition a return request from "
            f"'{return_request.status}' to '{to_status}'."
        )

    with transaction.atomic():
        ReturnRequestStatusHistory.objects.create(
            return_request=return_request,
            from_status=return_request.status,
            to_status=to_status,
            changed_by=changed_by,
            note=note,
        )
        return_request.status = to_status
        if to_status in _TERMINAL_STATUSES:
            return_request.resolved_at = timezone.now()
        return_request.save(update_fields=["status", "resolved_at"])
    return return_request


def _returnable_line(order, order_item_id):
    """Resolve the order line a return request applies to.

    A request names a specific line when given, otherwise the whole order is
    covered — which is only unambiguous when the order has a single line, so
    that line becomes the concrete restock/refund basis either way.

    Args:
        order (Order): the delivered order.
        order_item_id (int | None): an explicit line id, or None for a
            whole-order return.

    Returns:
        OrderItem: the concrete line the request covers.

    Raises:
        ValidationError: if the line is not part of the order, or a
            whole-order return is requested on a multi-line order.
    """
    if order_item_id is not None:
        order_item = order.items.filter(pk=order_item_id).first()
        if order_item is None:
            raise ValidationError(
                "The selected line item does not belong to this order."
            )
        return order_item

    line_count = order.items.count()
    if line_count > 1:
        raise ValidationError(
            "Select the specific line item to return for a multi-line order."
        )
    if line_count == 0:
        raise ValidationError("This order has no line items to return.")
    return order.items.first()


def create_return_request(
    *, order, order_item_id=None, reason, requested_resolution="refund", user=None
):
    """Open a post-delivery return request for an order line.

    Only a delivered order may be returned, and only within the cooling-off
    window configured on the site (measured in days from placement). A line is
    eligible when its product is still flagged returnable; a product deleted
    since the order is accepted because the flag can no longer be consulted.
    One request per line ever — open or already resolved — is enforced, so a
    retried client tap cannot create two requests for the same goods and a
    refunded line cannot be returned a second time.

    Args:
        order (Order): the delivered order being returned.
        order_item_id (int | None): the line to return, or None to cover a
            whole single-line order.
        reason (str): the customer's reason for the return.
        requested_resolution (str): the customer's requested resolution from
            ``ReturnRequest.RESOLUTION_CHOICES``.
        user (User | None): the acting user.

    Returns:
        ReturnRequest: the created request in ``requested`` status.

    Raises:
        ValidationError: if the order is not delivered, is outside the
            cooling-off window, the line is not returnable, or a request for
            the same line already exists or has been resolved.
    """
    if order.status != "delivered":
        raise ValidationError("Only a delivered order can be returned.")

    cooling_days = SiteConfig.load().cooling_off_period_days
    return_deadline = order.placed_at + timedelta(days=cooling_days)
    if timezone.now() > return_deadline:
        raise ValidationError(
            "Return requests must be opened within "
            f"{cooling_days} days of the order being placed."
        )

    order_item = _returnable_line(order, order_item_id)

    if order_item.product is not None and not order_item.product.is_returnable:
        raise ValidationError(
            f"'{order_item.product_name}' is not eligible for return."
        )

    # A rejected request leaves the return right intact; any other prior
    # request — open or refunded/replaced/closed — spends it. The database
    # constraint backs this up against a race.
    if (
        ReturnRequest.objects.filter(order=order, order_item=order_item)
        .exclude(status="rejected")
        .exists()
    ):
        raise ValidationError(
            "A return request for this item already exists or has been resolved."
        )

    with transaction.atomic():
        return_request = ReturnRequest.objects.create(
            order=order,
            order_item=order_item,
            reason=reason,
            requested_resolution=requested_resolution,
        )
        ReturnRequestStatusHistory.objects.create(
            return_request=return_request,
            from_status="",
            to_status="requested",
            changed_by=user,
        )
    return return_request


def _line_refund_base(order_item):
    """Return the money attributable to a returned line.

    The base is what the customer paid for the goods on that line plus the VAT
    charged on it — the line snapshots carry both, so the historical amount is
    used rather than the live catalogue price.

    Args:
        order_item (OrderItem): the line being returned.

    Returns:
        Decimal: the line total plus its tax, quantized to the penny.
    """
    return (_money(order_item.total_price) + _money(order_item.tax)).quantize(_PENNY)


def _restock_fee_percent(order_item):
    """Return the restocking fee percentage configured on the line's product.

    A product deleted since the order contributes no fee.

    Args:
        order_item (OrderItem): the line being returned.

    Returns:
        Decimal: the configured percentage, defaulting to zero.
    """
    if order_item.product is None:
        return Decimal("0.00")
    return _money(order_item.product.restocking_fee_percent)


def _quantize_amount(value):
    """Quantize a money value to the penny.

    Args:
        value: a raw money value.

    Returns:
        Decimal: ``value`` quantized to two decimal places.
    """
    return _money(value).quantize(_PENNY)


def approve_return_request(
    *,
    return_request,
    refund_method=None,
    refund_amount=None,
    restocking_fee_amount=None,
    user=None,
):
    """Approve a pending return request and fix its refund economics.

    For a refund resolution the restocking fee and refundable amount are
    computed here, server-side, from the order line snapshot and the product's
    configured fee percentage: the refund never exceeds the line total minus
    the fee, nor the order's grand total. An explicitly supplied fee or amount
    is honored only after the same caps. ``refund_method`` is a plain staff
    description of how the money will go back — it is recorded, never
    executed.

    Args:
        return_request (ReturnRequest): the requested request to approve.
        refund_method (str | None): how staff will send the money back, in
            staff words (e.g. "M-Pesa - sent manually").
        refund_amount (Decimal | None): an explicit refund amount; when absent
            the full eligible amount is used.
        restocking_fee_amount (Decimal | None): an explicit restocking fee;
            when absent the product's configured percentage is used.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the approved request.

    Raises:
        ValidationError: if the request is not requested, the refund method
            is missing, the restocking fee exceeds the line total, or an
            amount is negative or leaves nothing to refund.
    """
    if return_request.status != "requested":
        raise ValidationError("Only a requested return can be approved.")

    if return_request.requested_resolution in ("store_credit", "replacement"):
        raise ValidationError(
            "Store-credit and replacement resolutions are not available yet; "
            "approve a refund resolution instead."
        )

    refund_method = (refund_method or "").strip()[:100]
    if not refund_method:
        raise ValidationError(
            "Describe how the refund will be sent (e.g. 'M-Pesa - sent manually')."
        )

    base = _line_refund_base(return_request.order_item)
    if restocking_fee_amount is not None:
        fee = _quantize_amount(restocking_fee_amount)
        if fee < 0:
            raise ValidationError("A restocking fee cannot be negative.")
    else:
        fee = (
            base * _restock_fee_percent(return_request.order_item) / Decimal("100")
        ).quantize(_PENNY)

    if fee > base:
        raise ValidationError(
            f"The restocking fee cannot exceed the line total ({base})."
        )

    max_line_refund = (base - fee).quantize(_PENNY)

    collected = _money(return_request.order.grand_total)
    eligible = min(max_line_refund, collected).quantize(_PENNY)

    if refund_amount is not None:
        proposed = _quantize_amount(refund_amount)
        if proposed < 0:
            raise ValidationError("A refund amount cannot be negative.")
        if proposed > eligible:
            logger.warning(
                "Return %s refund capped from %s to eligible %s",
                return_request.pk,
                proposed,
                eligible,
            )
            proposed = eligible
    else:
        proposed = eligible

    if proposed <= 0:
        raise ValidationError(
            "The refund amount after the restocking fee would be zero; "
            "adjust the fee."
        )

    with transaction.atomic():
        return_request.restocking_fee_applied = fee
        return_request.refund_amount = proposed
        return_request.refund_method = refund_method
        return_request.save(
            update_fields=[
                "restocking_fee_applied",
                "refund_amount",
                "refund_method",
            ]
        )
        return transition_return_status(
            return_request,
            "approved",
            changed_by=user,
            note=(
                f"Approved as {refund_method}; restocking fee {fee}, "
                f"refund amount {proposed}."
            ),
        )


def record_item_received(*, return_request, user=None):
    """Record physical receipt of the returned goods.

    Moves the request to ``item_received`` in the same transaction as nothing
    else — there is no stock ledger to update, so receipt is purely the staff
    confirmation that the goods are back in hand.

    Args:
        return_request (ReturnRequest): the approved request.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the request in ``item_received`` status.

    Raises:
        ValidationError: if the request is not approved or the line can no
            longer be resolved.
    """
    if return_request.status != "approved":
        raise ValidationError(
            "Received goods can only be recorded for an approved return."
        )
    if return_request.order_item is None:
        raise ValidationError(
            "The returned line is no longer resolvable; reconcile the return "
            "before receiving goods."
        )

    with transaction.atomic():
        return transition_return_status(
            return_request,
            "item_received",
            changed_by=user,
            note="Goods received.",
        )


def refund_return_request(*, return_request, refund_note, user=None):
    """Record the staff-sent refund for a received return.

    The money only moves by staff hand (status must be ``item_received``), so
    this call records that it happened: the request is stamped ``refunded``
    with the staff note, and the order carries the refund amount and note for
    reporting. ``refund_note`` is required — an empty record of money going
    back is worse than none.

    Idempotent-safe and race-safe: the parent ``ReturnRequest`` row is locked
    with ``select_for_update`` at entry, and a request already ``refunded``
    is a no-op, so a retried call (under any idempotency key) records the
    refund exactly once.

    Args:
        return_request (ReturnRequest): the request whose goods were received.
        refund_note (str): what staff did (e.g. "M-Pesa sent, txn ABC123").
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the request in ``refunded`` status.

    Raises:
        ValidationError: if the request is not item-received, is not a refund
            resolution, carries no approved refund method, or the note is
            blank.
    """
    from apps.orders.services import _sanitize_plain

    note = _sanitize_plain(refund_note)
    if not note:
        raise ValidationError(
            "A refund note is required — record how the money went back."
        )

    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
        if locked.status == "refunded":
            logger.info(
                "Return %s is already refunded; skipping duplicate record.",
                locked.pk,
            )
            return locked
        if locked.status != "item_received":
            raise ValidationError(
                "Received goods must be recorded before a refund is recorded."
            )
        if locked.requested_resolution != "refund":
            raise ValidationError("Only a refund-resolution return can be refunded.")

        _require_credit_note_if_transmitted(locked.order)

        if not locked.refund_method:
            raise ValidationError(
                "No refund method was recorded when the return was approved."
            )

        transition_return_status(
            locked,
            "refunded",
            changed_by=user,
            note=f"Refund sent ({locked.refund_method}): {note}",
        )
        order = locked.order
        order.refund_amount = locked.refund_amount or Decimal("0.00")
        order.refund_note = note
        order.save(update_fields=["refund_amount", "refund_note"])
    _resolve_order_terminal_state(locked.order)
    return locked


def reject_return_request(*, return_request, note="", user=None):
    """Reject a return request before goods are restocked.

    Only a request that has not yet been received (or already completed) can
    be rejected; once goods are in hand the request must be resolved by
    refund or closure.

    Args:
        return_request (ReturnRequest): the request to reject.
        note (str): the staff reason, recorded for the customer and the audit
            trail.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the rejected request.

    Raises:
        ValidationError: if the request cannot be rejected from its current
            status.
    """
    if return_request.status not in ("requested", "approved"):
        raise ValidationError("Only a pending or approved return can be rejected.")
    return transition_return_status(
        return_request,
        "rejected",
        changed_by=user,
        note=note or "Return request rejected by staff.",
    )


def close_return_request(*, return_request, note="", user=None):
    """Close a return request without completing a refund or replacement.

    Terminal housekeeping for a request that will not reach a money-moving
    state — e.g. a customer withdraws the request or staff and customer agree
    to close the file.

    Args:
        return_request (ReturnRequest): the request to close.
        note (str): the reason for closing.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the closed request.

    Raises:
        ValidationError: if the request cannot be closed from its current
            status.
    """
    if return_request.status not in (
        "requested",
        "approved",
        "item_received",
        "refunded",
        "replaced",
    ):
        raise ValidationError("This return request cannot be closed.")
    return transition_return_status(
        return_request,
        "closed",
        changed_by=user,
        note=note or "Return request closed.",
    )


def _resolve_order_terminal_state(order):
    """Move a delivered order to a terminal state when every line is resolved.

    A delivered order leaves ``delivered`` only once all of its lines are
    covered by a completed return request, so a partial line-return leaves the
    order ``delivered`` while the individual request reads ``refunded``.
    Refunded lines leave the order ``refunded``; replacement-only resolution
    leaves it ``returned``.

    Args:
        order (Order): the delivered order being resolved.
    """
    if order.status != "delivered":
        return

    line_ids = set(order.items.values_list("pk", flat=True))
    if not line_ids:
        return

    completed_lines = set(
        ReturnRequest.objects.filter(
            order=order,
            status__in=("refunded", "replaced"),
            order_item_id__in=line_ids,
        ).values_list("order_item_id", flat=True)
    )
    if not line_ids <= completed_lines:
        return

    any_refunded = ReturnRequest.objects.filter(order=order, status="refunded").exists()
    to_status = "refunded" if any_refunded else "returned"
    transition_order(
        order,
        to_status,
        changed_by=None,
        note="All order lines have been returned.",
    )


def cancel_confirmed_order(
    *, order, user=None, note="", refund_note="", refund_amount=None
):
    """Cancel a confirmed-but-undelivered order.

    There is no stock to restock and no automated refund to trigger — this is
    a status change to ``cancelled`` plus the record of what happened. ``note``
    is required: it must say what happened and whether/how a refund was
    arranged manually. When money goes back to the customer, ``refund_note``
    says how and ``refund_amount`` says how much, both stored on the order for
    reporting.

    Idempotent-safe and race-safe: the ``Order`` row is locked with
    ``select_for_update`` and the status re-checked under the lock, so a
    second call — from a retry or a concurrent staff action carrying a
    different ``Idempotency-Key`` — rejects instead of recording twice.

    Args:
        order (Order): the confirmed or processing order.
        user (User | None): the acting staff user.
        note (str): required reason recorded in the audit trail.
        refund_note (str): how a manual refund was arranged, if any.
        refund_amount (Decimal | None): how much went back, if any.

    Returns:
        Order: the cancelled order.

    Raises:
        ValidationError: if the order is not confirmed/processing, the note
            is blank, or the refund amount is negative.
    """
    from apps.orders.services import _money, _sanitize_plain

    note = _sanitize_plain(note)
    if not note:
        raise ValidationError(
            "A cancellation note is required — record what happened and "
            "whether/how a refund was arranged."
        )
    refund_note = _sanitize_plain(refund_note)
    if refund_amount is None:
        refund_amount = Decimal("0.00")
    else:
        refund_amount = _money(refund_amount).quantize(_PENNY)
    if refund_amount < 0:
        raise ValidationError("A refund amount cannot be negative.")
    if order.status not in ("confirmed", "processing"):
        raise ValidationError(
            "Only a confirmed or processing order can be cancelled pre-shipment."
        )

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status not in ("confirmed", "processing"):
            raise ValidationError(
                "Only a confirmed or processing order can be cancelled pre-shipment."
            )

        _require_credit_note_if_transmitted(locked)

        locked.refund_note = refund_note
        locked.refund_amount = refund_amount
        locked.save(update_fields=["refund_note", "refund_amount"])

        return transition_order(
            locked,
            "cancelled",
            changed_by=user,
            note=note,
        )
