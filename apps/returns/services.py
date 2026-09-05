"""Business logic for the returns app.

The returns service is the only layer that creates or mutates ``ReturnRequest``
rows and the only place the post-delivery return and pre-shipment cancellation
workflows are implemented:

- ``create_return_request`` — a customer opens a request against a delivered
  order within the cooling-off window.
- ``approve_return_request`` — staff fixes the refund method and the
  server-computed amounts for a refund-resolution request.
- ``record_item_received`` — physical receipt restocks the line.
- ``refund_return_request`` — the money actually moves; the request is marked
  ``refunded`` only once it has.
- ``resolve_return_refund`` — the B2C-callback counterpart of the above.
- ``cancel_confirmed_order`` — the pre-shipment cancellation path: restocks,
  refunds what was collected, and cancels the order.

Invariants upheld here:

- ``ReturnRequest.status`` changes only through ``transition_return_status``,
  which writes a ``ReturnRequestStatusHistory`` row; ``Order.status`` changes
  only through the orders ``transition_order`` helper, which writes the order
  audit trail.
- Refund amounts are recomputed server-side from the order snapshots and
  capped at what was actually collected; a client-supplied amount is never
  charged, and a restocking fee can never exceed the line total.
- A request is never approved into a resolution that cannot be paid out today
  (store credit and replacement are rejected until their fulfilment ledgers
  exist), so no approved request can strand a customer's money or leave
  returned stock unaccounted for.
- A refund is recorded complete only when the money has moved: M-Pesa B2C
  requires Safaricom to confirm the payout (``resolve_return_refund``), a
  card reversal is recorded as a refunded ``Payment`` row.
- The refund and cancellation money paths lock their parent rows
  (``select_for_update``) so a duplicated or concurrent staff action can
  never fire two transfers.
- Returned stock goes back through the inventory restock path, never a direct
  ``Inventory`` edit, and serial-unit statuses move with the count ledger.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import SiteConfig
from apps.inventory.services import restock_returned_item
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
    the fee, nor what was actually collected for the order. An explicitly
    supplied fee or amount is honored only after the same caps. Resolutions
    that cannot be paid out from today's ledgers — store credit and
    replacement — are rejected here, so a request can never be approved with a
    refund that cannot be disbursed or a replacement whose shipment is
    unaccounted.

    Args:
        return_request (ReturnRequest): the requested request to approve.
        refund_method (str | None): the refund method for a refund-resolution
            request, from ``ReturnRequest.REFUND_METHOD_CHOICES``.
        refund_amount (Decimal | None): an explicit refund amount; when absent
            the full eligible amount is used.
        restocking_fee_amount (Decimal | None): an explicit restocking fee;
            when absent the product's configured percentage is used.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the approved request.

    Raises:
        ValidationError: if the request is not requested, the resolution is
            not refundable, the refund method is missing/invalid, the
            restocking fee exceeds the line total, or an amount is negative
            or leaves nothing to refund.
    """
    if return_request.status != "requested":
        raise ValidationError("Only a requested return can be approved.")

    if return_request.requested_resolution in ("store_credit", "replacement"):
        raise ValidationError(
            "Store-credit and replacement resolutions are not available yet; "
            "approve a refund resolution instead."
        )

    if not refund_method:
        raise ValidationError("A refund method is required for a refund resolution.")
    if refund_method not in dict(ReturnRequest.REFUND_METHOD_CHOICES):
        raise ValidationError(f"Unknown refund method '{refund_method}'.")
    if refund_method == "store_credit":
        raise ValidationError(
            "Store-credit refunds are not available yet; choose an M-Pesa B2C "
            "payout or a card reversal instead."
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

    from apps.payments.services import get_amount_collected_for_order

    collected = get_amount_collected_for_order(return_request.order)
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
    """Record physical receipt of the returned goods and restock the line.

    Restocks the returned line through the inventory restock path in the same
    transaction as the status change, so the count ledger never moves without
    the request state moving with it. Serialized products have their sold
    units moved to ``returned``; count-tracked products get the line quantity
    added back to the fulfilment warehouse.

    Args:
        return_request (ReturnRequest): the approved request.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the request in ``item_received`` status.

    Raises:
        ValidationError: if the request is not approved or the line cannot be
            restocked (e.g. its product was deleted or the fulfilment
            warehouse was dropped).
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
        restock_returned_item(order_item=return_request.order_item, user=user)
        return transition_return_status(
            return_request,
            "item_received",
            changed_by=user,
            note="Goods received and restocked.",
        )


def _record_card_reversal(order, amount, note):
    """Record a refunded card ``Payment`` row for a resolved refund.

    The card gateway is not wired into this checkout yet, so a card reversal
    is the ledger record of a manual gateway reversal performed by staff. The
    ``Payment`` row keeps the order's payment history reconstructable and
    lets the refunded amount be counted against the collected total.

    Args:
        order (Order): the order being refunded.
        amount (Decimal): the amount reversed.
        note (str): a free-text reference for the audit trail.
    """
    from apps.payments.models import Payment

    Payment.objects.create(
        order=order,
        provider="card",
        transaction_id="",
        amount=amount,
        status="refunded",
        raw_response={"note": note},
    )


def refund_return_request(*, return_request, user=None):
    """Resolve an item-received return by refunding the customer.

    The money only moves once the goods are in hand (status must be
    ``item_received``). For an M-Pesa B2C payout the payments service is
    called and the request stays ``item_received`` until Safaricom confirms
    the transfer, which drives ``resolve_return_refund``. A card reversal is
    recorded as a refunded ``Payment`` row and the request completes
    immediately. Store-credit resolution is rejected until the loyalty ledger
    exists.

    Idempotent-safe and race-safe: the parent ``ReturnRequest`` row is locked
    with ``select_for_update`` at entry, so concurrent staff actions (which
    may carry different ``Idempotency-Key`` values) serialize on the request
    and a payout already pending or confirmed for this request causes a no-op
    — a duplicate transfer can never be initiated.

    Args:
        return_request (ReturnRequest): the request whose goods were received.
        user (User | None): the acting staff user.

    Returns:
        ReturnRequest: the request, ``refunded`` (card) or still
            ``item_received`` awaiting the B2C callback.

    Raises:
        ValidationError: if the request is not item-received, is not a refund
            resolution, or carries no approved refund method.
    """
    if return_request.status != "item_received":
        raise ValidationError(
            "Received goods must be recorded before a refund is initiated."
        )
    if return_request.requested_resolution != "refund":
        raise ValidationError("Only a refund-resolution return can be refunded.")

    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
        if locked.status != "item_received":
            raise ValidationError(
                "Received goods must be recorded before a refund is initiated."
            )

        refund_method = locked.refund_method
        if not refund_method:
            raise ValidationError(
                "No refund method was recorded when the return was approved."
            )

        if refund_method == "store_credit":
            raise ValidationError(
                "Store-credit refunds are not available yet; choose an M-Pesa "
                "B2C payout or a card reversal instead."
            )

        if refund_method == "mpesa_b2c":
            from apps.payments.models import MpesaB2CPayout
            from apps.payments.services import initiate_b2c_refund

            existing = MpesaB2CPayout.objects.filter(
                return_request=locked,
                status__in=("pending", "success"),
            ).exists()
            if existing:
                logger.info(
                    "Return %s already has a B2C payout in flight; skipping.",
                    locked.pk,
                )
                return locked

            initiate_b2c_refund(
                locked.order,
                locked.refund_amount,
                reason="return_refund",
                return_request=locked,
            )
            return locked

        if refund_method == "card_reversal":
            _record_card_reversal(
                locked.order,
                locked.refund_amount,
                note=(
                    f"Card reversal for return request {locked.pk} "
                    f"of {locked.refund_amount}."
                ),
            )
            transition_return_status(
                locked,
                "refunded",
                changed_by=user,
                note=f"Refunded by card reversal of {locked.refund_amount}.",
            )
            _resolve_order_terminal_state(locked.order)
            return locked

    raise ValidationError(f"Unknown refund method '{refund_method}'.")


def resolve_return_refund(payout):
    """Mark a return request refunded once its B2C payout is confirmed.

    Called from the payment layer when Safaricom confirms a B2C transfer
    linked to a return. Idempotent-safe: a request already refunded (or a
    payout that failed) is a no-op. On success the request is stamped
    ``refunded`` and the order moves to ``refunded`` once every line is
    covered.

    Args:
        payout (MpesaB2CPayout): the confirmed payout.
    """
    if payout.status != "success":
        logger.warning(
            "Refusing to resolve return from payout %s in status %s",
            payout.pk,
            payout.status,
        )
        return

    return_request = payout.return_request
    if return_request is None:
        logger.warning(
            "Payout %s has no linked return request; nothing to resolve.",
            payout.pk,
        )
        return
    if return_request.status == "refunded":
        return

    if return_request.status != "item_received":
        logger.warning(
            "Return %s is in status %s while its payout %s succeeded; "
            "refusing to resolve out of order.",
            return_request.pk,
            return_request.status,
            payout.pk,
        )
        return

    with transaction.atomic():
        transition_return_status(
            return_request,
            "refunded",
            note=(
                "Refund confirmed by M-Pesa B2C payout "
                f"{payout.conversation_id[:12] or 'unknown'}."
            ),
        )
    _resolve_order_terminal_state(return_request.order)


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


def cancel_confirmed_order(*, order, user=None, note=""):
    """Cancel a confirmed-but-undelivered order and refund what was collected.

    Restocks every line and issues the refund in one transaction. Money that
    was actually collected pre-delivery is refunded — an M-Pesa order through
    a B2C payout with reason ``order_cancellation``, a card order through a
    recorded reversal — while a COD order has collected nothing pre-delivery
    and gets no refund. The order then moves to ``cancelled``. A failed payout
    aborts the whole cancellation, so stock is never restocked for an order
    that is not actually cancelled.

    Idempotent-safe and race-safe: the ``Order`` row is locked with
    ``select_for_update`` and the status re-checked under the lock, so a
    second call — from a retry or a concurrent staff action carrying a
    different ``Idempotency-Key`` — rejects or sees the existing pending/
    success cancellation payout rather than firing a duplicate transfer.

    Args:
        order (Order): the confirmed or processing order.
        user (User | None): the acting staff user.
        note (str): the reason recorded in the audit trail.

    Returns:
        Order: the cancelled order.

    Raises:
        ValidationError: if the order is not confirmed/processing or uses an
            unsupported payment method.
    """
    if order.status not in ("confirmed", "processing"):
        raise ValidationError(
            "Only a confirmed or processing order can be cancelled " "pre-shipment."
        )
    if order.payment_method not in ("mpesa", "card", "cod"):
        raise ValidationError(
            "Orders using this payment method cannot be cancelled " "pre-shipment."
        )

    from apps.payments.models import MpesaB2CPayout
    from apps.payments.services import (
        get_amount_collected_for_order,
        initiate_b2c_refund,
    )

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status not in ("confirmed", "processing"):
            raise ValidationError(
                "Only a confirmed or processing order can be cancelled " "pre-shipment."
            )
        if locked.payment_method not in ("mpesa", "card", "cod"):
            raise ValidationError(
                "Orders using this payment method cannot be cancelled " "pre-shipment."
            )

        for order_item in locked.items.select_related("product"):
            restock_returned_item(order_item=order_item, user=user)

        collected = get_amount_collected_for_order(locked)

        if locked.payment_method == "mpesa" and collected > 0:
            existing = MpesaB2CPayout.objects.filter(
                order=locked,
                reason="order_cancellation",
                status__in=("pending", "success"),
            ).exists()
            if not existing:
                initiate_b2c_refund(
                    locked,
                    collected,
                    reason="order_cancellation",
                )
        elif locked.payment_method == "card" and collected > 0:
            _record_card_reversal(
                locked,
                collected,
                note="Manual card reversal recorded for pre-shipment cancellation.",
            )

        return transition_order(
            locked,
            "cancelled",
            changed_by=user,
            note=note or "Cancelled before shipment.",
        )
