"""Data models for the payments app.

``MpesaTransaction`` tracks every STK Push initiated through the Daraja API,
one row per checkout request.  The ``checkout_request_id`` is the unique
deduplication key — Safaricom may deliver the same callback multiple times,
and the service layer no-ops on a record already in a terminal state before
mutating anything.

``Payment`` is a provider-agnostic ledger entry for every confirmed payment
against an order.  It exists so a single order can carry multiple partial
payments (e.g. a partial M-Pesa top-up plus a COD balance) without losing
the provenance of each.

``MpesaB2CPayout`` tracks outbound B2C transfers used for refunds and
returns.  ``conversation_id`` is the Safaricom-assigned deduplication key;
the service layer checks it before initiating a duplicate transfer.
"""

from django.db import models


class MpesaTransaction(models.Model):
    """An STK Push transaction initiated through the Safaricom Daraja API.

    Each checkout request produces exactly one row.  ``checkout_request_id``
    is unique and is the idempotency anchor for inbound callbacks — the
    service layer looks up the transaction by this key and skips any mutation
    when the record is already ``success``, ``failed``, ``cancelled``, or
    ``timeout``.

    ``raw_callback`` stores the full Daraja callback body for audit and
    debugging; it is never exposed over the API.
    """

    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled by user"),
        ("timeout", "Timeout"),
    )

    order = models.ForeignKey(
        "orders.Order",
        related_name="mpesa_transactions",
        on_delete=models.CASCADE,
    )
    phone_number = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    checkout_request_id = models.CharField(max_length=100, unique=True)
    merchant_request_id = models.CharField(max_length=100, blank=True)
    mpesa_receipt_number = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    result_code = models.CharField(max_length=10, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    raw_callback = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Set when a payment is received (status ``success``) but the order could
    # not be confirmed because it was no longer pending — e.g. the stock-
    # reservation sweep cancelled it while the STK callback was still in
    # flight.  Money arrived with nothing to attach to, so an operator must
    # reconcile rather than us silently dropping or forcing the confirmation.
    needs_reconciliation = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["checkout_request_id"],
                name="mpesa_checkout_req_idx",
            ),
            models.Index(
                fields=["order", "status"],
                name="mpesa_order_status_idx",
            ),
            models.Index(fields=["status"], name="mpesa_status_idx"),
            models.Index(fields=["created_at"], name="mpesa_created_idx"),
        ]

    def __str__(self):
        """Return a compact label with status and masked phone."""
        from apps.accounts.services import mask_phone

        return (
            f"M-Pesa {self.checkout_request_id} "
            f"({self.status}) {mask_phone(self.phone_number)}"
        )


class MpesaB2CPayout(models.Model):
    """An outbound B2C transfer, used for refunds and returns.

    ``conversation_id`` is the Safaricom-assigned idempotency key — a
    duplicate ``conversation_id`` on an inbound callback means the same
    transfer, and the service layer must not process it twice.

    ``raw_callback`` stores the full Daraja B2C callback body for audit.
    """

    REASON_CHOICES = (
        ("return_refund", "Return/Refund"),
        ("order_cancellation", "Order Cancellation Refund"),
        ("other", "Other"),
    )
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("success", "Success"),
        ("failed", "Failed"),
    )

    order = models.ForeignKey(
        "orders.Order",
        related_name="b2c_payouts",
        on_delete=models.CASCADE,
    )
    reason = models.CharField(
        max_length=20, choices=REASON_CHOICES, default="return_refund"
    )
    phone_number = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    conversation_id = models.CharField(max_length=100, unique=True)
    originator_conversation_id = models.CharField(max_length=100, blank=True)
    mpesa_receipt_number = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    raw_callback = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["conversation_id"],
                name="b2c_conversation_idx",
            ),
            models.Index(
                fields=["order", "status"],
                name="b2c_order_status_idx",
            ),
        ]

    def __str__(self):
        """Return a compact label with status and masked phone."""
        from apps.accounts.services import mask_phone

        return (
            f"B2C {self.conversation_id[:12]}… "
            f"({self.status}) {mask_phone(self.phone_number)}"
        )


class Payment(models.Model):
    """A provider-agnostic payment record for an order.

    Every confirmed payment against an order — regardless of method — produces
    a ``Payment`` row so the order's payment history is reconstructable without
    digging into provider-specific tables.  An order may carry multiple partial
    payments; the sum of ``amount`` across all ``Payment`` rows for an order
    is the total amount paid to date.

    ``raw_response`` stores the provider's original response for audit; it is
    never exposed over the public API.
    """

    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
    )

    order = models.ForeignKey(
        "orders.Order",
        related_name="payments",
        on_delete=models.CASCADE,
    )
    provider = models.CharField(max_length=50)
    transaction_id = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    raw_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order"], name="payment_order_idx"),
            models.Index(fields=["provider", "status"], name="payment_prov_status_idx"),
        ]

    def __str__(self):
        """Return a compact label with provider and status."""
        return f"Payment {self.pk} ({self.provider}/{self.status})"
