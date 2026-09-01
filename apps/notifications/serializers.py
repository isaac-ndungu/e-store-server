"""Serializers for the notifications app.

Defines the writable payload for the internal test-send endpoint and the
read-only representation returned in audit-log queries.  All write
serializers use an explicit field list so no privileged fields (``status``,
``provider_message_id``, ``sent_by``) can be injected by the caller.
"""

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


class NotificationLogSerializer(serializers.ModelSerializer):
    """Read-only representation of a ``NotificationLog`` for audit queries.

    Exposes all meaningful fields as read-only so the API consumer can
    inspect the outcome of any send without being able to mutate the log.
    """

    sent_by_email = serializers.CharField(
        source="sent_by.email", read_only=True, default=None
    )

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
            "sent_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
