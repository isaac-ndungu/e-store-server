from django.urls import path

from apps.payments.views import (
    MpesaB2CCallbackView,
    MpesaSTKCallbackView,
    MpesaTransactionStatusView,
)

urlpatterns = [
    path(
        "payments/mpesa/stk-callback/",
        MpesaSTKCallbackView.as_view(),
        name="mpesa-stk-callback",
    ),
    path(
        "payments/mpesa/b2c-callback/",
        MpesaB2CCallbackView.as_view(),
        name="mpesa-b2c-callback",
    ),
    path(
        "payments/mpesa/transactions/<int:transaction_id>/",
        MpesaTransactionStatusView.as_view(),
        name="mpesa-transaction-status",
    ),
]
