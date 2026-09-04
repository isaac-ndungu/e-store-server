"""API serializers for the payments app.

Read serializers expose M-Pesa transaction and payment status to the caller
without leaking provider secrets, raw callback payloads, or PII beyond what
the order itself already carries.

No writable input serializers exist here — the STK Push is triggered by the
order creation flow, not by a standalone endpoint the client calls with an
amount.
"""

from rest_framework import serializers

from apps.payments.models import MpesaB2CPayout, MpesaTransaction, Payment


class MpesaTransactionSerializer(serializers.ModelSerializer):
    """Read-only detail of an M-Pesa STK Push transaction.

    Exposes status and receipt number so the storefront can poll for
    confirmation.  ``raw_callback`` and ``result_desc`` are excluded to
    avoid leaking internal provider details.
    """

    class Meta:
        model = MpesaTransaction
        fields = [
            "id",
            "order",
            "phone_number",
            "amount",
            "checkout_request_id",
            "mpesa_receipt_number",
            "status",
            "result_code",
            "created_at",
            "confirmed_at",
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    """Read-only detail of a generic payment record."""

    class Meta:
        model = Payment
        fields = [
            "id",
            "order",
            "provider",
            "transaction_id",
            "amount",
            "status",
            "created_at",
        ]
        read_only_fields = fields


class MpesaB2CPayoutSerializer(serializers.ModelSerializer):
    """Read-only detail of a B2C refund payout."""

    class Meta:
        model = MpesaB2CPayout
        fields = [
            "id",
            "order",
            "reason",
            "phone_number",
            "amount",
            "conversation_id",
            "mpesa_receipt_number",
            "status",
            "created_at",
        ]
        read_only_fields = fields
