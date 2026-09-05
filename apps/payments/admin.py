"""Admin configuration for the payments app.

Read-only admin views for monitoring M-Pesa transactions, B2C payouts, and
generic payment records.  All fields are read-only in the admin — payment
records are created exclusively by the service layer.
"""

from django.contrib import admin

from apps.payments.models import MpesaB2CPayout, MpesaTransaction, Payment


@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(admin.ModelAdmin):
    """Read-only admin view for STK Push transactions."""

    list_display = (
        "checkout_request_id",
        "order",
        "phone_number",
        "amount",
        "status",
        "mpesa_receipt_number",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("checkout_request_id", "mpesa_receipt_number", "phone_number")
    readonly_fields = (
        "order",
        "phone_number",
        "amount",
        "checkout_request_id",
        "merchant_request_id",
        "mpesa_receipt_number",
        "status",
        "result_code",
        "result_desc",
        "raw_callback",
        "created_at",
        "confirmed_at",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "order",
                    "phone_number",
                    "amount",
                    "status",
                )
            },
        ),
        (
            "Daraja IDs",
            {
                "fields": (
                    "checkout_request_id",
                    "merchant_request_id",
                    "mpesa_receipt_number",
                )
            },
        ),
        (
            "Result",
            {
                "fields": ("result_code", "result_desc", "confirmed_at"),
            },
        ),
        ("Audit", {"fields": ("raw_callback", "created_at"), "classes": ("collapse",)}),
    )


@admin.register(MpesaB2CPayout)
class MpesaB2CPayoutAdmin(admin.ModelAdmin):
    """Read-only admin view for B2C refund payouts."""

    list_display = (
        "conversation_id",
        "order",
        "return_request",
        "phone_number",
        "amount",
        "reason",
        "status",
        "mpesa_receipt_number",
        "created_at",
    )
    list_filter = ("status", "reason")
    search_fields = ("conversation_id", "mpesa_receipt_number", "phone_number")
    readonly_fields = (
        "order",
        "return_request",
        "reason",
        "phone_number",
        "amount",
        "conversation_id",
        "originator_conversation_id",
        "mpesa_receipt_number",
        "status",
        "raw_callback",
        "created_at",
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Read-only admin view for generic payment records."""

    list_display = (
        "id",
        "order",
        "provider",
        "transaction_id",
        "amount",
        "status",
        "created_at",
    )
    list_filter = ("provider", "status")
    search_fields = ("transaction_id",)
    readonly_fields = (
        "order",
        "provider",
        "transaction_id",
        "amount",
        "status",
        "raw_response",
        "created_at",
    )
