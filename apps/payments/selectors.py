"""Read-only query helpers for the payments app.

Selectors encapsulate query construction so views and serializers never build
raw querysets directly.  They are pure reads with no side effects.
"""

from apps.payments.models import MpesaB2CPayout, MpesaTransaction, Payment


def get_mpesa_transaction_by_checkout_id(checkout_request_id):
    """Return the M-Pesa transaction matching a Daraja checkout request ID.

    Used by the callback handler to look up the transaction before processing.
    A missing ID returns None so the caller can 404 without leaking whether
    the transaction exists.

    Args:
        checkout_request_id (str): the Daraja checkout request ID.

    Returns:
        MpesaTransaction | None: the transaction, or None.
    """
    return (
        MpesaTransaction.objects.filter(checkout_request_id=checkout_request_id)
        .select_related("order")
        .first()
    )


def get_mpesa_transaction_for_user(user, transaction_id):
    """Return an M-Pesa transaction that belongs to the user's order.

    Ownership is resolved through the order's ``user`` FK so a caller can
    only see their own payment transactions.  Returns None for a missing or
    another user's transaction so the caller can 404.

    Args:
        user (User): the authenticated user.
        transaction_id (int): the MpesaTransaction primary key.

    Returns:
        MpesaTransaction | None: the transaction, or None.
    """
    return (
        MpesaTransaction.objects.filter(pk=transaction_id, order__user=user)
        .select_related("order")
        .first()
    )


def list_mpesa_transactions_for_order(order):
    """Return M-Pesa transactions for an order.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the transactions ordered newest-first.
    """
    return MpesaTransaction.objects.filter(order=order).order_by("-created_at")


def list_payments_for_order(order):
    """Return payment records for an order.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the payment records ordered newest-first.
    """
    return Payment.objects.filter(order=order).order_by("-created_at")


def get_b2c_payout_by_conversation(conversation_id):
    """Return a B2C payout by its Safaricom conversation ID.

    Used by the B2C callback handler for idempotent lookup.

    Args:
        conversation_id (str): the Safaricom conversation ID.

    Returns:
        MpesaB2CPayout | None: the payout, or None.
    """
    return (
        MpesaB2CPayout.objects.filter(conversation_id=conversation_id)
        .select_related("order")
        .first()
    )
