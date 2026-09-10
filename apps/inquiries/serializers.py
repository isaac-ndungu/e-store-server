"""Serializers for the inquiries app.

The public serializer whitelists exactly what an anonymous visitor may send
(channel + cart snapshot + optional contact hint). Staff serializers expose
the queue fields and the status-transition input.
"""

from rest_framework import serializers

from apps.inquiries.models import Inquiry

MAX_SNAPSHOT_LINES = 100


def _validate_snapshot(value):
    """Validate the cart snapshot shape.

    Args:
        value: the raw ``cart_snapshot`` payload.

    Returns:
        list: the validated snapshot lines.

    Raises:
        serializers.ValidationError: if the shape is wrong.
    """
    if not isinstance(value, list) or not value:
        raise serializers.ValidationError("cart_snapshot must be a non-empty list.")
    if len(value) > MAX_SNAPSHOT_LINES:
        raise serializers.ValidationError(
            f"cart_snapshot holds at most {MAX_SNAPSHOT_LINES} lines."
        )
    cleaned = []
    for line in value:
        if not isinstance(line, dict):
            raise serializers.ValidationError("Each snapshot line must be an object.")
        sku = line.get("sku", "")
        name = line.get("name", "")
        quantity = line.get("quantity")
        if not isinstance(sku, str) or not sku or len(sku) > 100:
            raise serializers.ValidationError("Each line needs a valid sku.")
        if not isinstance(name, str) or len(name) > 255:
            raise serializers.ValidationError("Each line needs a valid name.")
        if not isinstance(quantity, int) or quantity < 1 or quantity > 999:
            raise serializers.ValidationError("Each line needs a quantity 1-999.")
        price = line.get("price", "")
        if price != "" and not isinstance(price, (str, int, float)):
            raise serializers.ValidationError("Line price must be a string or number.")
        cleaned.append(
            {
                "sku": sku,
                "name": name,
                "quantity": quantity,
                "price": str(price) if price != "" else "",
            }
        )
    return cleaned


class InquiryCreateSerializer(serializers.Serializer):
    """Input for the public hand-off capture endpoint."""

    channel = serializers.ChoiceField(choices=Inquiry.CHANNEL_CHOICES)
    cart_snapshot = serializers.JSONField()
    contact_hint = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )

    def validate_cart_snapshot(self, value):
        """Validate the snapshot lines.

        Args:
            value: the raw snapshot payload.

        Returns:
            list: the cleaned snapshot lines.
        """
        return _validate_snapshot(value)


class InquiryDetailSerializer(serializers.ModelSerializer):
    """Staff-facing read shape for an inquiry."""

    class Meta:
        model = Inquiry
        fields = [
            "id",
            "channel",
            "cart_snapshot",
            "contact_hint",
            "status",
            "converted_order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class InquiryStatusUpdateSerializer(serializers.Serializer):
    """Input for a staff user moving an inquiry through the queue."""

    to_status = serializers.ChoiceField(choices=Inquiry.STATUS_CHOICES)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
