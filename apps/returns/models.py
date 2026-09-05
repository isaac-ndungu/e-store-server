"""Data models for the returns app.

A ``ReturnRequest`` captures a post-delivery return of one order line (or a
whole single-line order) from the customer's request through staff approval,
physical receipt, and refund resolution. ``status`` is the single source of
truth for where a request is; the returns service layer records every change
in ``ReturnRequestStatusHistory`` alongside it, so the resolution trail —
including the amounts computed at approval — is fully reconstructable.

The upstream model sketch also lists an optional link to a support ticket.
The support module is not built yet, so the field is intentionally absent
here; it will be added by a follow-up migration on this app once the target
model exists.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint


class ReturnRequest(models.Model):
    """A customer's request to return an order line after delivery.

    ``order_item`` is nullable because a request may cover a whole single-line
    order; it resolves to a specific delivered line whenever the order has
    more than one. ``order`` is never nullable — a request is always tied to
    the order it originated from, which is also what refunds route against.

    ``refund_amount`` is computed and stored by the service layer at approval
    time, never accepted from a client. ``restocking_fee_applied`` records the
    fee actually deducted so the amount paid out always reconciles against the
    line total.
    """

    STATUS_CHOICES = (
        ("requested", "Requested"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("item_received", "Item Received"),
        ("refunded", "Refunded"),
        ("replaced", "Replaced"),
        ("closed", "Closed"),
    )
    RESOLUTION_CHOICES = (
        ("refund", "Refund"),
        ("replacement", "Replacement"),
        ("store_credit", "Store Credit"),
    )
    REFUND_METHOD_CHOICES = (
        ("mpesa_b2c", "M-Pesa B2C Payout"),
        ("card_reversal", "Card Reversal"),
        ("store_credit", "Store Credit"),
    )

    order = models.ForeignKey(
        "orders.Order",
        related_name="return_requests",
        on_delete=models.CASCADE,
    )
    order_item = models.ForeignKey(
        "orders.OrderItem",
        null=True,
        blank=True,
        related_name="return_requests",
        on_delete=models.SET_NULL,
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="requested"
    )
    requested_resolution = models.CharField(
        max_length=20, choices=RESOLUTION_CHOICES, default="refund"
    )
    restocking_fee_applied = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    refund_method = models.CharField(
        max_length=20, choices=REFUND_METHOD_CHOICES, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order"], name="return_order_idx"),
            models.Index(fields=["order_item"], name="return_order_item_idx"),
            models.Index(
                fields=["status", "created_at"], name="return_status_created_idx"
            ),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(restocking_fee_applied__gte=0)
                & (Q(refund_amount__isnull=True) | Q(refund_amount__gte=0)),
                name="return_money_fields_nonnegative",
            ),
            # A rejected request does not spend the right to return the line;
            # every other status — including a completed refund — does, so a
            # physical line can never carry more than one live-or-resolved
            # request. The service layer enforces the same rule for an
            # understandable error message; this is the race-proof backstop.
            models.UniqueConstraint(
                fields=["order", "order_item"],
                condition=~Q(status="rejected") & Q(order_item__isnull=False),
                name="return_one_per_line_not_rejected",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the return request."""
        return f"Return #{self.pk} for order {self.order_id} ({self.status})"


class ReturnRequestStatusHistory(models.Model):
    """An audit entry for one return-request status transition.

    Written by the returns service layer every time ``ReturnRequest.status``
    changes so the resolution trail is fully reconstructable — including the
    refund method and computed amount recorded at the terminal transition.
    """

    return_request = models.ForeignKey(
        ReturnRequest, related_name="status_history", on_delete=models.CASCADE
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="return_status_changes",
    )
    note = models.TextField(blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at", "pk"]
        indexes = [
            models.Index(
                fields=["return_request", "changed_at"], name="rsh_req_changed_idx"
            ),
        ]

    def __str__(self):
        """Return a compact label with the transition."""
        return f"{self.from_status or '(created)'} -> {self.to_status}"
