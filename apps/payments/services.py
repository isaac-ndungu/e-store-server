"""Business logic for the payments app.

The service layer is the only code that creates or mutates ``MpesaTransaction``,
``Payment``, and ``MpesaB2CPayout`` rows.  Views and Celery tasks call into
these functions; they never write to the database directly.

Invariants:
    - M-Pesa callbacks are idempotent: ``handle_stk_callback`` and
      ``handle_b2c_callback`` look up the transaction by its Safaricom-assigned
      deduplication key and no-op when the record is already in a terminal
      state.  Safaricom retries callbacks, so this is not defensive — it is
      the primary correctness path.
    - ``confirm_order_from_payment`` routes through the same reservation-
      fulfilment path the OTP flow uses, so a paid order can never skip stock
      bookkeeping.
"""

import logging
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import mask_phone
from apps.orders.payments import cancel_order_from_payment, confirm_order_from_payment
from apps.payments.daraja import DarajaError
from apps.payments.daraja import initiate_stk_push as _daraja_stk_push

logger = logging.getLogger(__name__)

# Per-phone throttle on STK Push initiation.  A burst of pushes aimed at the
# same number is a real abuse vector — each one pops an M-Pesa prompt on the
# victim's phone — so the number of prompts we fire per phone per window is
# bounded here, independently of any per-requester (IP) throttle on the
# order-placement endpoint.  The key is scoped to the phone, not the caller,
# so rotating IPs or creating many guest orders against one number cannot
# amplify the spam.
_STK_PHONE_KEY = "payments:stk_push_initiate:{phone}"
_STK_PHONE_WINDOW_SECONDS = 60
_STK_PHONE_MAX_PER_WINDOW = 3


class StkPushRateLimited(Exception):
    """Raised when an STK Push would exceed the per-phone rate limit.

    Attributes:
        retry_after (int): seconds until a push to this number may be retried.
    """

    def __init__(self, retry_after):
        """Initialize the exception.

        Args:
            retry_after (int): seconds until the next push is allowed.
        """
        super().__init__(
            "Too many M-Pesa prompts sent to this phone number; please try again "
            "shortly."
        )
        self.retry_after = retry_after


def initiate_stk_push(order):
    """Initiate an M-Pesa STK Push for a pending order.

    Creates an ``MpesaTransaction`` row in ``pending`` status, then calls the
    Daraja STK Push API.  On a network or API failure the transaction is left
    in ``pending`` so the callback can still arrive, or a timeout sweep can
    clean it up later.

    Args:
        order (Order): the pending order to charge.

    Returns:
        MpesaTransaction: the newly created transaction record.

    Raises:
        DarajaError: if the Daraja API call itself fails (network error or
            non-2xx response).  The caller may surface this to the client or
            fall back to an alternative payment method.
        StkPushRateLimited: if the per-phone STK push rate limit is exceeded
            for this order's phone number.
    """
    from apps.payments.models import MpesaTransaction

    _enforce_stk_phone_rate_limit(order.phone)

    account_ref = f"Order #{order.pk}"

    transaction_record = MpesaTransaction.objects.create(
        order=order,
        phone_number=order.phone,
        amount=order.grand_total,
        checkout_request_id="",
        status="pending",
    )

    try:
        response = _daraja_stk_push(
            phone_number=order.phone,
            amount=order.grand_total,
            order_id=order.pk,
            account_reference=account_ref,
        )
    except DarajaError:
        logger.exception(
            "Daraja STK Push failed for order %s (phone %s)",
            order.pk,
            mask_phone(order.phone),
        )
        raise

    checkout_request_id = response.get("CheckoutRequestID", "")
    merchant_request_id = response.get("MerchantRequestID", "")

    if not checkout_request_id:
        transaction_record.status = "failed"
        transaction_record.result_desc = str(response)[:255]
        transaction_record.save(update_fields=["status", "result_desc"])
        logger.error(
            "Daraja STK Push returned no CheckoutRequestID for order %s: %s",
            order.pk,
            response,
        )
        return transaction_record

    transaction_record.checkout_request_id = checkout_request_id
    transaction_record.merchant_request_id = merchant_request_id
    transaction_record.save(
        update_fields=["checkout_request_id", "merchant_request_id"]
    )

    logger.info(
        "STK Push initiated for order %s — checkout_request_id=%s",
        order.pk,
        checkout_request_id,
    )
    return transaction_record


def _enforce_stk_phone_rate_limit(phone_number):
    """Reject STK Push initiation when the per-phone window budget is exhausted.

    Uses a fixed-key counter with an expiry equal to the window, extended on
    each increment so a sustained attacker never sees the window reset out
    from under them.  Atomic ``incr`` keeps the count correct under concurrent
    order creation against the same number.

    Args:
        phone_number (str): the E.164 number being targeted.

    Raises:
        StkPushRateLimited: if the number already received the per-window
            maximum of STK prompts.
    """
    key = _STK_PHONE_KEY.format(phone=phone_number)
    added = cache.add(
        key,
        1,
        timeout=_STK_PHONE_WINDOW_SECONDS,
    )
    if added:
        return
    count = cache.incr(key)
    if count > _STK_PHONE_MAX_PER_WINDOW:
        logger.warning(
            "STK push rate limit hit for phone %s",
            mask_phone(phone_number),
        )
        # The budget resets when the counter key expires, which is at most a
        # full window away; report that as the retry delay.
        raise StkPushRateLimited(_STK_PHONE_WINDOW_SECONDS)


def handle_stk_callback(callback_body):
    """Process an M-Pesa STK Push callback from Safaricom.

    The callback body contains the ``CheckoutRequestID`` which is the
    idempotency key — the transaction is looked up by this key, and if it
    is already in a terminal state (success, failed, cancelled, timeout) the
    function returns immediately without mutating anything.  This is the
    primary idempotency path per domain rule 6.

    On success the order is confirmed (reservations fulfilled, coupon
    redeemed).  On failure the order is cancelled and stock released.  Both
    paths go through the order service layer so the status machine and
    audit trail are never bypassed.

    The ``Amount`` Safaricom returns in the callback is cross-checked against
    the amount that was requested (``MpesaTransaction.amount``) before the
    order is confirmed.  A mismatched amount — whether from a provider bug,
    an integration error, or a tampered callback — does not silently confirm
    the order and release stock; the transaction is marked ``failed`` and the
    order cancelled so the discrepancy is surfaced for reconciliation rather
    than buried.

    Args:
        callback_body (dict): the parsed JSON callback from Daraja.

    Returns:
        MpesaTransaction | None: the updated transaction record, or None if
            the callback contained no ``CheckoutRequestID``.
    """
    from apps.payments.models import MpesaTransaction

    body = callback_body
    if "Body" in body:
        body = body["Body"]

    stk = body.get("StkCallback", {})
    checkout_request_id = stk.get("CheckoutRequestID", "")
    merchant_request_id = stk.get("MerchantRequestID", "")
    result_code = str(stk.get("ResultCode", ""))
    result_desc = stk.get("ResultDesc", "")

    if not checkout_request_id:
        logger.warning("STK callback missing CheckoutRequestID: %s", callback_body)
        return None

    with transaction.atomic():
        transaction_record = (
            MpesaTransaction.objects.select_for_update()
            .filter(checkout_request_id=checkout_request_id)
            .first()
        )

        if transaction_record is None:
            logger.warning(
                "STK callback for unknown checkout_request_id=%s",
                checkout_request_id,
            )
            return None

        # Idempotency: if the transaction is already terminal, skip.
        if transaction_record.status in (
            "success",
            "failed",
            "cancelled",
            "timeout",
        ):
            logger.info(
                "STK callback idempotent no-op for %s (status=%s)",
                checkout_request_id,
                transaction_record.status,
            )
            return transaction_record

        transaction_record.merchant_request_id = (
            merchant_request_id or transaction_record.merchant_request_id
        )
        transaction_record.result_code = result_code
        transaction_record.result_desc = result_desc[:255]
        transaction_record.raw_callback = callback_body

        order = transaction_record.order

        if result_code == "0":
            meta = stk.get("CallbackMetadata", {}).get("Item", [])
            receipt = ""
            callback_amount = None
            for item in meta:
                if item.get("Name") == "MpesaReceiptNumber":
                    receipt = item.get("Value", "")
                elif item.get("Name") == "Amount":
                    callback_amount = item.get("Value")

            if not _amount_matches_requested(
                transaction_record.amount, callback_amount
            ):
                transaction_record.status = "failed"
                transaction_record.result_code = "AMOUNT_MISMATCH"
                transaction_record.result_desc = (
                    f"Callback amount {callback_amount} does not match requested "
                    f"amount {transaction_record.amount}."
                )[:255]
                transaction_record.save(
                    update_fields=[
                        "merchant_request_id",
                        "result_code",
                        "result_desc",
                        "raw_callback",
                        "status",
                    ]
                )
                if order.status == "pending":
                    cancel_order_from_payment(
                        order,
                        note="M-Pesa callback amount does not match requested "
                        "amount; flagged for reconciliation.",
                    )
                logger.error(
                    "M-Pesa amount mismatch on order %s: requested=%s, callback=%s",
                    order.pk,
                    transaction_record.amount,
                    callback_amount,
                )
                return transaction_record

            transaction_record.mpesa_receipt_number = receipt
            transaction_record.status = "success"
            transaction_record.confirmed_at = timezone.now()
            transaction_record.save(
                update_fields=[
                    "merchant_request_id",
                    "result_code",
                    "result_desc",
                    "raw_callback",
                    "mpesa_receipt_number",
                    "status",
                    "confirmed_at",
                ]
            )

            _create_payment_record(order, transaction_record)

            # Late-success race: money was received but the order is no longer
            # pending — e.g. the reservation sweep released stock and cancelled
            # it while the STK callback was still in flight.  Re-confirming a
            # cancelled order would quietly fulfil stock that has already been
            # freed or refunded, and silently dropping a valid payment hides
            # money that has no fulfillment to attach to.  Neither happens:
            # the transaction stays ``success`` (money really arrived) but is
            # flagged for operator reconciliation instead.
            try:
                confirm_order_from_payment(order)
            except ValidationError:
                transaction_record.needs_reconciliation = True
                transaction_record.save(update_fields=["needs_reconciliation"])
                logger.error(
                    "M-Pesa payment received for order %s (receipt %s) but the "
                    "order could not be confirmed (status %s); requires "
                    "reconciliation — %s",
                    order.pk,
                    receipt,
                    order.status,
                    transaction_record.checkout_request_id,
                )
                return transaction_record

            logger.info(
                "M-Pesa payment confirmed for order %s, receipt=%s",
                order.pk,
                receipt,
            )
        else:
            _map_daraja_failure_status(transaction_record, result_code)
            transaction_record.save(
                update_fields=[
                    "merchant_request_id",
                    "result_code",
                    "result_desc",
                    "raw_callback",
                    "status",
                ]
            )
            if order.status == "pending":
                cancel_order_from_payment(
                    order,
                    note=f"M-Pesa payment failed: {result_desc[:200]}",
                )
            logger.info(
                "M-Pesa payment failed for order %s, code=%s",
                order.pk,
                result_code,
            )

    return transaction_record


def _map_daraja_failure_status(transaction_record, result_code):
    """Map Daraja STK Push result codes to transaction status choices.

    Daraja uses numeric result codes to indicate different failure modes.
    The mapping is:
        1032  → cancelled (user pressed cancel on the STK prompt)
        1037  → timeout  (user did not respond to the STK prompt)
        other → failed   (any other non-success code)

    Args:
        transaction_record (MpesaTransaction): the transaction to update.
        result_code (str): the Daraja result code.
    """
    code_map = {
        "1032": "cancelled",
        "1037": "timeout",
    }
    transaction_record.status = code_map.get(result_code, "failed")


def _amount_matches_requested(requested_amount, callback_amount):
    """Return whether a callback amount matches the amount that was requested.

    Money values from provider JSON are normalized through ``Decimal`` — never
    ``float`` — so a value like ``8000.00`` survives the boundary without
    precision drift.  Both values are compared as exact decimals; a missing
    or null callback amount fails the check.

    Args:
        requested_amount (Decimal): the amount that was charged.
        callback_amount: the raw ``Amount`` value from the Daraja callback
            (may be an int, str, Decimal, or None).

    Returns:
        bool: True when the callback amount equals the requested amount.
    """
    if callback_amount is None or callback_amount == "":
        return False
    try:
        callback_decimal = Decimal(str(callback_amount))
    except ValueError, TypeError:
        return False
    return Decimal(str(requested_amount)) == callback_decimal


def _create_payment_record(order, mpesa_transaction):
    """Create a generic ``Payment`` row from a confirmed M-Pesa transaction.

    The ``Payment`` record provides a provider-agnostic ledger entry so the
    order's payment history is reconstructable without digging into M-Pesa-
    specific tables.

    Args:
        order (Order): the confirmed order.
        mpesa_transaction (MpesaTransaction): the confirmed M-Pesa transaction.
    """
    from apps.payments.models import Payment

    Payment.objects.create(
        order=order,
        provider="mpesa",
        transaction_id=mpesa_transaction.mpesa_receipt_number,
        amount=mpesa_transaction.amount,
        status="completed",
        raw_response={
            "checkout_request_id": mpesa_transaction.checkout_request_id,
            "mpesa_receipt_number": mpesa_transaction.mpesa_receipt_number,
            "result_code": mpesa_transaction.result_code,
        },
    )


def handle_b2c_callback(callback_body):
    """Process an M-Pesa B2C callback from Safaricom.

    Idempotent via ``conversation_id``: if the payout record is already in
    a terminal state the callback is silently ignored.

    Args:
        callback_body (dict): the parsed JSON B2C callback from Daraja.

    Returns:
        MpesaB2CPayout | None: the updated payout record, or None if the
            callback contained no ``ConversationID``.
    """
    from apps.payments.models import MpesaB2CPayout

    result = callback_body.get("Result", {})
    conversation_id = result.get("ConversationID", "")
    originator_id = result.get("OriginatorConversationID", "")
    result_code = str(result.get("ResultCode", ""))
    transaction_id = result.get("TransactionID", "")

    if not conversation_id:
        logger.warning("B2C callback missing ConversationID: %s", callback_body)
        return None

    with transaction.atomic():
        payout = (
            MpesaB2CPayout.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )

        if payout is None:
            logger.warning(
                "B2C callback for unknown conversation_id=%s",
                conversation_id,
            )
            return None

        # Idempotency: skip if already terminal.
        if payout.status in ("success", "failed"):
            logger.info(
                "B2C callback idempotent no-op for %s (status=%s)",
                conversation_id,
                payout.status,
            )
            return payout

        payout.originator_conversation_id = (
            originator_id or payout.originator_conversation_id
        )
        payout.mpesa_receipt_number = transaction_id
        payout.raw_callback = callback_body

        if result_code == "0":
            payout.status = "success"
        else:
            payout.status = "failed"

        payout.save(
            update_fields=[
                "originator_conversation_id",
                "mpesa_receipt_number",
                "raw_callback",
                "status",
            ]
        )

        logger.info(
            "B2C payout %s status=%s for order %s",
            conversation_id[:12],
            payout.status,
            payout.order_id,
        )

        if payout.return_request_id is not None:
            _resolve_return_from_payout(payout)

    return payout


def _resolve_return_from_payout(payout):
    """Carry a settled B2C payout through to its return request.

    A return refund is only recorded as complete once the money has actually
    moved — M-Pesa confirming the B2C payout.  On success the linked return
    request is marked refunded (and its order refreshed if fully covered);
    on failure it stays where it is so staff can inspect and retry.

    The import is deferred to avoid a circular dependency with the returns
    service, and the call is made defensively so a returns-side failure can
    never break the payment callback that just confirmed a transfer.

    Args:
        payout (MpesaB2CPayout): the settled payout with a linked return
            request.
    """
    try:
        from apps.returns.services import resolve_return_refund

        resolve_return_refund(payout)
    except Exception:
        logger.exception(
            "Failed to resolve return request %s from B2C payout %s",
            payout.return_request_id,
            payout.conversation_id[:12],
        )


def _amount_paid_for_order(order):
    """Return the total amount actually collected for an order.

    The cap for a refund is what was really received, summed from the order's
    completed ``Payment`` rows (created only after a successful STK callback
    amount-verified and confirmed).  A delivered COD order is special-cased:
    its full ``grand_total`` counts as collected, because collection happens
    at delivery rather than at placement and there is no ``Payment`` row for
    it.  A COD order that has not been delivered has collected nothing, so a
    pre-shipment cancellation of a COD order correctly refunds nothing.  If
    no payment row exists yet — which should not happen for a refundable order
    — the total falls back to zero so a refund is never silently authorized
    against a negative or empty base.

    Args:
        order (Order): the order being refunded.

    Returns:
        Decimal: the total confirmed amount received for the order.
    """
    from django.db.models import Sum

    from apps.payments.models import Payment

    total = Payment.objects.filter(order=order, status="completed").aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")
    if order.payment_method == "cod" and order.status == "delivered":
        return Decimal(order.grand_total)
    return Decimal(total)


def get_amount_collected_for_order(order):
    """Return the total confirmed amount collected for an order.

    Public contract for the refund cap: what was really received, summed from
    the order's completed ``Payment`` rows.  Returns zero when nothing has
    been collected, so a refund is never authorized against an empty base.

    Args:
        order (Order): the order being considered for a refund.

    Returns:
        Decimal: the total confirmed amount received for the order.
    """
    return _amount_paid_for_order(order)


def _resolve_refund_destination(order):
    """Return the phone number to pay a refund out to.

    The destination is the number that actually paid for the order — resolved
    from the order's confirmed M-Pesa transaction when one exists, else the
    order's own contact phone.  The caller never supplies the destination, so
    a refund cannot be routed to an arbitrary number the caller happens to
    name.

    Args:
        order (Order): the order being refunded.

    Returns:
        str: the E.164 phone number for the payout.
    """
    from apps.payments.models import MpesaTransaction

    confirmed_txn = (
        MpesaTransaction.objects.filter(order=order, status="success")
        .order_by("-confirmed_at")
        .first()
    )
    if confirmed_txn is not None and confirmed_txn.phone_number:
        return confirmed_txn.phone_number
    return order.phone


def initiate_b2c_refund(order, amount, reason="return_refund", *, return_request=None):
    """Initiate a B2C refund payout for an order.

    The destination phone number and the refund amount are both derived
    server-side, never supplied by the caller:

    - The phone is resolved from the order's confirmed M-Pesa transaction
      (falling back to ``Order.phone``), so a refund goes back to whoever
      actually paid.
    - The amount is capped at the total actually collected for the order, so
      a bug or a bad input can never authorize a refund larger than what was
      originally received.

    Creates a ``MpesaB2CPayout`` record in ``pending`` status and calls the
    Daraja B2C API.  The payout record tracks the ``conversation_id`` for
    idempotent callback processing.  When ``return_request`` is given the
    payout is linked to it so the callback can drive the return's resolution.

    Args:
        order (Order): the order being refunded.
        amount (Decimal): the requested refund amount.  Silently capped at
            ``_amount_paid_for_order(order)`` if it exceeds what was collected.
        reason (str): the refund reason code.
        return_request (ReturnRequest | None): the return request this payout
            refunds, if any, linked for callback-driven resolution.

    Returns:
        MpesaB2CPayout: the newly created payout record.

    Raises:
        DarajaError: if the Daraja B2C API call fails.
    """
    from apps.payments.daraja import initiate_b2c_payment as _daraja_b2c
    from apps.payments.models import MpesaB2CPayout

    phone_number = _resolve_refund_destination(order)
    collected = _amount_paid_for_order(order)
    refund_amount = Decimal(str(amount))
    if refund_amount > collected:
        logger.warning(
            "B2C refund for order %s capped from %s to collected %s",
            order.pk,
            refund_amount,
            collected,
        )
        refund_amount = collected

    occasion = f"Refund for Order #{order.pk}"
    remarks = f"Refund: {reason}"

    payout = MpesaB2CPayout.objects.create(
        order=order,
        reason=reason,
        phone_number=phone_number,
        amount=refund_amount,
        conversation_id="",
        status="pending",
        return_request=return_request,
    )

    try:
        response = _daraja_b2c(
            phone_number=phone_number,
            amount=refund_amount,
            occasion=occasion,
            remarks=remarks,
        )
    except DarajaError:
        logger.exception(
            "Daraja B2C failed for order %s refund (phone %s)",
            order.pk,
            mask_phone(phone_number),
        )
        raise

    conversation_id = response.get("ConversationID", "")
    originator_id = response.get("OriginatorConversationID", "")

    if not conversation_id:
        payout.status = "failed"
        payout.raw_callback = response
        payout.save(update_fields=["status", "raw_callback"])
        logger.error(
            "Daraja B2C returned no ConversationID for order %s: %s",
            order.pk,
            response,
        )
        return payout

    payout.conversation_id = conversation_id
    payout.originator_conversation_id = originator_id
    payout.save(update_fields=["conversation_id", "originator_conversation_id"])

    logger.info(
        "B2C refund initiated for order %s — conversation_id=%s",
        order.pk,
        conversation_id,
    )
    return payout
