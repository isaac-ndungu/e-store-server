import bleach
from rest_framework import serializers

from apps.notifications.models import NotificationLog


class SendTestSMSSerializer(serializers.Serializer):
    """Validate the phone number and message for an internal test SMS.

    Both fields are required.  The phone number must be a valid E.164
    number; the message is capped at 1600 characters (Africa's Talking
    supports concatenated messages up to this length).
    """

    recipient = serializers.CharField(
        max_length=15,
        help_text="E.164 phone number, e.g. +254712345678.",
    )
    message = serializers.CharField(
        max_length=1600,
        help_text="SMS body text (max 1600 characters).",
    )

    def validate_recipient(self, value):
        """Normalise and validate the phone number to E.164 form.

        Args:
            value (str): the raw phone number.

        Returns:
            str: the validated E.164 number.

        Raises:
            serializers.ValidationError: if the number is not valid E.164.
        """
        from apps.accounts.services import validate_phone_number

        return validate_phone_number(value)

    def validate_message(self, value):
        """Strip any HTML tags from the message body.

        Currently SMS-only (plain text), but the model declares ``email``
        as a channel choice.  Sanitising proactively prevents an XSS
        vector if the message is ever rendered as HTML downstream.

        Args:
            value (str): the raw message text.

        Returns:
            str: the sanitised message text.
        """
        return bleach.clean(value, tags=set(), strip=True)


class _SenderSerializer(serializers.Serializer):
    """Minimal nested representation of the staff user who sent a notification."""

    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)


class NotificationLogSerializer(serializers.ModelSerializer):
    """Read-only representation of a ``NotificationLog`` for audit queries.

    ``provider_response`` is intentionally omitted from ``fields`` — raw
    provider payloads may contain sensitive infrastructure details (API
    keys in request echoes, internal IDs).  Ops staff can inspect them
    through the Django admin where the field is exposed as read-only.

    ``error_message`` is masked for non-superuser callers (A1) so raw
    provider rejection strings don't leak implementation details through
    the API.  Superusers see the full error for debugging.
    """

    sent_by = _SenderSerializer(read_only=True)

    class Meta:
        model = NotificationLog
        fields = [
            "id",
            "channel",
            "purpose",
            "recipient",
            "message",
            "status",
            "provider_message_id",
            "error_message",
            "sent_by",
            "segments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        """Mask ``error_message`` for non-superuser callers.

        Args:
            instance: the ``NotificationLog`` instance.

        Returns:
            dict: the serialised representation.
        """
        data = super().to_representation(instance)
        request = self.context.get("request")
        if instance.error_message and request and not request.user.is_superuser:
            data["error_message"] = "Send failed. See Django admin for details."
        return data
