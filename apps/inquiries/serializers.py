"""Serializers for the inquiries app.

The public serializer whitelists exactly what an anonymous visitor may send
(channel + optional contact hint). The cart snapshot is read server-side
from the visitor's cookie-linked cart; a client-submitted snapshot is only
accepted as a fallback when no server cart exists (e.g. cookies cleared),
and is validated line by line. Staff serializers expose the queue fields
and the status-transition input.
"""

import re

import bleach
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.services import normalize_email, validate_phone_number
from apps.inquiries.models import Inquiry

MAX_SNAPSHOT_LINES = 100
MAX_ORDER_NOTES = 2000
MIN_FORM_FILL_SECONDS = 2
KENYAN_MOBILE_PATTERN = re.compile(r"^\+254[17][0-9]{8}$")


def _clean_text(value, *, max_length=None):
    """Return plain text with markup and surrounding whitespace removed.

    Args:
        value (str): the raw submitted text.
        max_length (int | None): optional defensive length cap.

    Returns:
        str: the sanitized text.
    """
    cleaned = bleach.clean(value or "", tags=set(), strip=True).strip()
    if max_length is not None:
        cleaned = cleaned[:max_length]
    return cleaned


def _normalize_phone(value):
    """Normalize a Kenyan mobile number to ``+254`` form.

    Args:
        value (str): the submitted phone number.

    Returns:
        str: the normalized E.164 number, or an empty string when blank.

    Raises:
        serializers.ValidationError: when the number is not a Kenyan mobile.
    """
    raw = (value or "").strip()
    if raw == "":
        return ""
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) == 9 and digits[0] in "17":
        raw = f"0{digits}"
    normalized = validate_phone_number(raw)
    if not KENYAN_MOBILE_PATTERN.match(normalized):
        raise serializers.ValidationError(
            "Enter a Kenyan mobile number, e.g. 0712345678 or +254712345678."
        )
    return normalized



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
    cart_snapshot = serializers.JSONField(required=False, default=None)
    contact_hint = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    customer_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    customer_phone = serializers.CharField(
        max_length=30, required=False, allow_blank=True, default=""
    )
    customer_email = serializers.EmailField(
        max_length=254, required=False, allow_blank=True, default=""
    )
    delivery_location = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    order_notes = serializers.CharField(
        max_length=MAX_ORDER_NOTES, required=False, allow_blank=True, default=""
    )
    reference_code = serializers.CharField(
        max_length=16, required=False, allow_blank=True, default=""
    )
    page_url = serializers.URLField(
        max_length=500, required=False, allow_blank=True, default=""
    )
    is_test = serializers.BooleanField(
        required=False, default=False, write_only=True
    )
    company_website = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
        write_only=True,
    )
    form_started_at = serializers.DateTimeField(
        required=False, allow_null=True, default=None, write_only=True
    )

    def validate_contact_hint(self, value):
        """Sanitize the optional contact hint.

        Args:
            value (str): the raw contact hint.

        Returns:
            str: the sanitized contact hint.
        """
        return _clean_text(value, max_length=255)

    def validate_customer_name(self, value):
        """Sanitize the optional visitor name."""
        return _clean_text(value, max_length=120)

    def validate_customer_phone(self, value):
        """Normalize the optional mobile number to ``+254`` form."""
        return _normalize_phone(value)

    def validate_customer_email(self, value):
        """Normalize the optional reply-to email."""
        return normalize_email(value) if value else ""

    def validate_delivery_location(self, value):
        """Sanitize the optional delivery area or landmark."""
        return _clean_text(value, max_length=255)

    def validate_order_notes(self, value):
        """Sanitize the optional order notes."""
        return _clean_text(value, max_length=MAX_ORDER_NOTES)

    def validate_cart_snapshot(self, value):
        """Validate the fallback snapshot lines.

        Args:
            value: the raw snapshot payload, or None when the server
                cart supplies the lines.

        Returns:
            list | None: the cleaned snapshot lines, or None.
        """
        if value is None:
            return None
        cleaned = _validate_snapshot(value)
        for line in cleaned:
            if "name" in line:
                line["name"] = bleach.clean(
                    line["name"], tags=set(), strip=True
                ).strip()
            if "sku" in line:
                line["sku"] = bleach.clean(line["sku"], tags=set(), strip=True).strip()
        return cleaned

    def validate(self, attrs):
        """Reject obvious bot submissions without storing a row.

        The hidden field is never shown to a person, and a form filled faster
        than the minimum interval is treated the same way. Both checks stay
        optional so older storefront builds remain compatible during a
        staggered deploy.
        """
        if attrs.get("company_website"):
            raise serializers.ValidationError("This submission could not be accepted.")
        started_at = attrs.get("form_started_at")
        if started_at is not None:
            elapsed = (timezone.now() - started_at).total_seconds()
            if 0 <= elapsed < MIN_FORM_FILL_SECONDS:
                raise serializers.ValidationError(
                    "This submission could not be accepted."
                )
        return attrs


class InquiryDetailSerializer(serializers.ModelSerializer):
    """Staff-facing read shape for an inquiry."""

    reference = serializers.ReadOnlyField()
    message_text = serializers.SerializerMethodField()

    class Meta:
        model = Inquiry
        fields = [
            "id",
            "reference",
            "channel",
            "cart_snapshot",
            "contact_hint",
            "customer_name",
            "customer_phone",
            "customer_email",
            "delivery_location",
            "order_notes",
            "page_url",
            "status",
            "message_received_at",
            "email_status",
            "email_error",
            "email_provider_message_id",
            "email_attempts",
            "email_last_attempt_at",
            "email_sent_at",
            "converted_order",
            "message_text",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_message_text(self, obj):
        """Return the canonical pre-filled message for this inquiry.

        Args:
            obj (Inquiry): the serialized row.

        Returns:
            str: the text the storefront encodes into its hand-off link.
        """
        from apps.inquiries.services import build_handoff_message

        return build_handoff_message(
            cart_snapshot=obj.cart_snapshot,
            reference=obj.reference,
            page_url=obj.page_url,
        )


class InquiryStatusUpdateSerializer(serializers.Serializer):
    """Input for a staff user moving an inquiry through the queue."""

    to_status = serializers.ChoiceField(choices=Inquiry.STATUS_CHOICES)


class InquiryRecoveryContactSerializer(serializers.Serializer):
    """Input for attaching optional name and phone from the recovery box."""

    customer_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    customer_phone = serializers.CharField(
        max_length=30, required=False, allow_blank=True, default=""
    )
    recovery_token = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default="", write_only=True
    )

    def validate_customer_name(self, value):
        """Sanitize the optional visitor name."""
        return _clean_text(value, max_length=120)

    def validate_customer_phone(self, value):
        """Normalize the optional Kenyan mobile number to ``+254`` form."""
        return _normalize_phone(value)

    def validate(self, attrs):
        """Require at least one contact field when submitting the recovery form."""
        if not attrs.get("customer_name") and not attrs.get("customer_phone"):
            raise serializers.ValidationError(
                "Provide a name or phone number to save."
            )
        return attrs


class InquiryMarkReceivedSerializer(serializers.Serializer):
    """Input for marking an inquiry as message_received by reference code."""

    reference = serializers.CharField(max_length=32)

    def validate_reference(self, value):
        """Strip surrounding whitespace from the scanned or pasted reference."""
        cleaned = _clean_text(value, max_length=32)
        if not cleaned:
            raise serializers.ValidationError("Enter a reference code.")
        return cleaned


class InquiryBulkSpamSerializer(serializers.Serializer):
    """Input for bulk-marking test or spam inquiries."""

    inquiry_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        min_length=1,
        max_length=200,
    )
