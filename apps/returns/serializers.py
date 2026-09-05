"""API serializers for the returns app.

Writable serializers whitelist exactly the fields a caller may supply:
the customer names the line, reason, and requested resolution for a new
request; staff name the refund method (and optionally an explicit refund or
restocking-fee amount) at approval. No client-supplied money value is ever
treated as authoritative — the service caps every amount against the order
snapshot and the collected total.

Read serializers expose the request state, the computed economics, and the
audit trail; the refund amount and restocking fee are read-only.
"""

import bleach
from rest_framework import serializers

from apps.orders.models import OrderItem
from apps.returns.models import ReturnRequest, ReturnRequestStatusHistory


class ReturnOrderItemSerializer(serializers.ModelSerializer):
    """Compact read-only summary of the order line being returned."""

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "variant_sku",
            "product_name",
            "quantity",
            "unit_price",
            "total_price",
        ]
        read_only_fields = fields


class ReturnRequestCreateSerializer(serializers.Serializer):
    """Input for a customer opening a return request.

    ``order_item_id`` is optional for a whole single-line order; ``reason`` is
    free-form customer text; ``requested_resolution`` names the resolution the
    customer wants.
    """

    order_item_id = serializers.IntegerField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=2000)
    requested_resolution = serializers.ChoiceField(
        choices=ReturnRequest.RESOLUTION_CHOICES, default="refund"
    )

    def validate_reason(self, value):
        """Strip any HTML markup from the customer's reason for return.

        The reason is stored and later rendered in staff/admin surfaces;
        sanitizing at the edge prevents a stored-XSS vector.

        Args:
            value (str): the raw reason text.

        Returns:
            str: the sanitised reason text.
        """
        return bleach.clean(value, tags=set(), strip=True)


class ReturnRequestStatusHistorySerializer(serializers.ModelSerializer):
    """Read-only detail for one return-request status transition (staff view).

    Exposes the acting staff member and the free-form note; this shape is only
    served to staff. The customer-facing detail uses a version without either.
    """

    class Meta:
        model = ReturnRequestStatusHistory
        fields = [
            "from_status",
            "to_status",
            "changed_by",
            "note",
            "changed_at",
        ]
        read_only_fields = fields


class ReturnRequestListSerializer(serializers.ModelSerializer):
    """List-view shape exposing a compact return summary."""

    class Meta:
        model = ReturnRequest
        fields = [
            "id",
            "order",
            "order_item",
            "reason",
            "status",
            "requested_resolution",
            "refund_method",
            "refund_amount",
            "restocking_fee_applied",
            "resolved_at",
            "created_at",
        ]


class ReturnRequestDetailSerializer(serializers.ModelSerializer):
    """Detail-view shape with the line, resolution, and audit trail."""

    order_item = ReturnOrderItemSerializer(read_only=True)
    status_history = ReturnRequestStatusHistorySerializer(many=True, read_only=True)

    class Meta:
        model = ReturnRequest
        fields = [
            "id",
            "order",
            "order_item",
            "reason",
            "status",
            "requested_resolution",
            "refund_method",
            "refund_amount",
            "restocking_fee_applied",
            "resolved_at",
            "created_at",
            "status_history",
        ]


class ReturnApproveSerializer(serializers.Serializer):
    """Input for a staff user approving a return request.

    ``refund_method`` is required for a refund resolution and validated against
    the choices; the service re-checks it against the requested resolution.
    ``refund_amount`` and ``restocking_fee`` are optional staff overrides —
    when absent, the service computes them from the order snapshot, and any
    supplied value is still capped server-side.
    """

    refund_method = serializers.ChoiceField(
        choices=ReturnRequest.REFUND_METHOD_CHOICES,
        required=False,
        allow_blank=True,
    )
    refund_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    restocking_fee = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )


class ReturnRejectSerializer(serializers.Serializer):
    """Input for rejecting or closing a return request.

    ``note`` is free-form staff text recorded in the audit trail and returned
    to the customer in the detail view.
    """

    note = serializers.CharField(required=False, allow_blank=True)

    def validate_note(self, value):
        """Strip any HTML markup from the staff note.

        The note is stored and returned to the customer over the API, so it is
        sanitized at the edge.

        Args:
            value (str): the raw note text.

        Returns:
            str: the sanitised note text.
        """
        return bleach.clean(value, tags=set(), strip=True)


class PreShipmentCancelSerializer(serializers.Serializer):
    """Input for the pre-shipment cancellation of a confirmed order.

    ``note`` is free-form staff text recorded in the order's status-history
    audit trail.
    """

    note = serializers.CharField(required=False, allow_blank=True)

    def validate_note(self, value):
        """Strip any HTML markup from the cancellation note.

        The note is stored and shown on the order detail; sanitizing at the
        edge prevents a stored-XSS vector.

        Args:
            value (str): the raw note text.

        Returns:
            str: the sanitised note text.
        """
        return bleach.clean(value, tags=set(), strip=True)


class ReturnRequestCustomerStatusHistorySerializer(serializers.ModelSerializer):
    """Read-only transition history served to the customer.

    ``changed_by`` and the free-form staff ``note`` are internal and are never
    exposed to the order owner — a customer only sees which statuses the
    request moved through and when.
    """

    class Meta:
        model = ReturnRequestStatusHistory
        fields = [
            "from_status",
            "to_status",
            "changed_at",
        ]
        read_only_fields = fields


class ReturnRequestCustomerDetailSerializer(serializers.ModelSerializer):
    """Detail shape served to the order owner.

    Mirrors the staff detail view without ``changed_by`` or staff notes in the
    history trail, so an authenticated owner or a guest holding the lookup
    token can never learn who on the team handled their request or read
    internal notes.
    """

    order_item = ReturnOrderItemSerializer(read_only=True)
    status_history = ReturnRequestCustomerStatusHistorySerializer(
        many=True, read_only=True
    )

    class Meta:
        model = ReturnRequest
        fields = [
            "id",
            "order",
            "order_item",
            "reason",
            "status",
            "requested_resolution",
            "refund_method",
            "refund_amount",
            "restocking_fee_applied",
            "resolved_at",
            "created_at",
            "status_history",
        ]
