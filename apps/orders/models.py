"""Data models for the orders app.

An ``Order`` captures everything needed to fulfil and bill a purchase
independently of the live catalogue. Line totals, tax rates, and product
details are snapshotted onto ``OrderItem`` at checkout time so a historical
order renders exactly as it was charged even after catalogue prices or spec
sheets change.

The status field is the single source of truth for where an order is in its
lifecycle; every transition must be recorded in ``OrderStatusHistory`` by the
service layer, never written directly in a view.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint


class Order(models.Model):
    """A purchase with its shipping, payment, and fulfilment details.

    ``phone`` is the order-level contact number — the number staff confirm
    payment and delivery against. ``user`` is null for assisted sales (there
    are no customer accounts); ``staff_created_by`` records which staff
    member created the order.
    ``status`` tracks the order through the fulfilment pipeline; the service
    layer records every change in ``OrderStatusHistory`` alongside it.
    ``delivery_fee`` is the amount staff quoted the customer for delivery in
    the sales conversation, typed in at intake — no fee table stands behind
    it. ``delivery_area`` records where the order is going. ``refund_note``
    and ``refund_amount`` record money returned to the customer when an order
    is cancelled or returned; refunds are arranged manually by staff, so
    these fields are the record rather than a payout reference.
    """

    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("processing", "Processing"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("delivery_failed", "Delivery Failed"),
        ("cancelled", "Cancelled"),
        ("refunded", "Refunded"),
        ("returned", "Returned"),
    )
    PAYMENT_METHOD_CHOICES = (
        ("mpesa", "M-Pesa (confirmed manually by staff)"),
        ("cod", "Cash on Delivery"),
        ("bank_transfer", "Bank Transfer"),
    )
    ORDER_SOURCE_CHOICES = (
        ("whatsapp", "WhatsApp"),
        ("email", "Email"),
        ("admin_manual", "Staff-Entered"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )
    lookup_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    phone = models.CharField(max_length=15)
    email = models.EmailField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES, default="mpesa"
    )
    order_source = models.CharField(
        max_length=20, choices=ORDER_SOURCE_CHOICES, default="whatsapp"
    )
    staff_created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders_created",
    )
    payment_reference = models.CharField(max_length=100, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    shipping_tax_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    loyalty_points_redeemed = models.PositiveIntegerField(default=0)
    coupon = models.ForeignKey(
        "promotions.Coupon",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )
    grand_total = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_address = models.ForeignKey(
        "accounts.Address",
        related_name="shipping_orders",
        null=True,
        on_delete=models.SET_NULL,
    )
    delivery_area = models.ForeignKey(
        "shipping.DeliveryArea",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )
    refund_note = models.TextField(blank=True)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    placed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-placed_at"]
        indexes = [
            models.Index(fields=["user", "placed_at"], name="order_user_placed_idx"),
            models.Index(fields=["phone"], name="order_phone_idx"),
            models.Index(
                fields=["status", "placed_at"], name="order_status_placed_idx"
            ),
            models.Index(
                fields=["payment_method", "status"], name="order_pmt_status_idx"
            ),
            models.Index(fields=["order_source"], name="order_source_idx"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(subtotal__gte=0)
                & Q(delivery_fee__gte=0)
                & Q(tax_total__gte=0)
                & Q(grand_total__gte=0)
                & Q(refund_amount__gte=0),
                name="order_money_fields_nonnegative",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the order."""
        return f"Order #{self.pk} ({self.status})"

    @property
    def items_total(self):
        """Return the sum of all line-item total prices.

        This is used to reconcile the order's money fields, never to charge
        the customer — the authoritative amounts are the snapshot fields on
        the order.

        Returns:
            Decimal: the sum of ``total_price`` across all line items.
        """
        from decimal import Decimal

        total = self.items.aggregate(total=models.Sum(models.F("total_price")))["total"]
        return total if total is not None else Decimal("0")


class OrderItem(models.Model):
    """One line of an order with order-time data snapshotted.

    ``product`` is nullable (``SET_NULL``) so a deleted catalogue product does
    not destroy the line; ``product_name``, ``variant_sku``, ``variant_attributes``
    and the money fields are copied at checkout time so the historical order
    renders independently of live catalogue state.

    ``bundle_group_id`` groups the component lines that resulted from a single
    bundle purchase. When a bundle is bought, each component becomes its own
    ``OrderItem`` sharing a common UUID; a standalone product line has no
    ``bundle_group_id``.
    """

    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(
        "catalog.Product",
        null=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
    )
    bundle = models.ForeignKey(
        "bundles.Bundle",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
    )
    bundle_group_id = models.UUIDField(null=True, blank=True)
    variant_sku = models.CharField(max_length=100)
    product_name = models.CharField(max_length=255)
    variant_attributes = models.JSONField(default=dict)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)
    # The per-line amount discounted by a promotion/action at checkout time
    # (excluding any coupon), snapshotted so redemptions and historical order
    # economics are reconstructable without re-pricing live catalogue state.
    applied_discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["order"], name="order_item_order_idx"),
            models.Index(
                fields=["order", "bundle_group_id"], name="order_item_bundle_idx"
            ),
            models.Index(fields=["variant_sku"], name="order_item_sku_idx"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(quantity__gte=1)
                & Q(unit_price__gte=0)
                & Q(total_price__gte=0),
                name="order_item_money_and_qty_nonnegative",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the line."""
        return f"{self.product_name} ({self.variant_sku}) x{self.quantity}"


class OrderStatusHistory(models.Model):
    """An audit entry for one order status transition.

    Written by the order service layer every time ``Order.status`` changes so
    the full pipeline is reconstructable. ``from_status`` is blank for the
    initial ``pending`` row created when the order is placed.
    """

    order = models.ForeignKey(
        Order, related_name="status_history", on_delete=models.CASCADE
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_status_changes",
    )
    note = models.TextField(blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at", "pk"]
        indexes = [
            models.Index(fields=["order", "changed_at"], name="osh_order_changed_idx"),
        ]

    def __str__(self):
        """Return a compact label with the transition."""
        return f"{self.from_status or '(created)'} -> {self.to_status}"


def new_bundle_group_id():
    """Return a fresh, unique bundle-group identifier.

    A bundle purchase decomposes into a set of per-component ``OrderItem``
    rows that share this value so the lines can be grouped back together for
    display and analytics.

    Returns:
        UUID: a new universally unique identifier.
    """
    return uuid.uuid4()
