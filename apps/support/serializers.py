"""API serializers for the support app.

Write serializers whitelist exactly the fields a caller may supply; the author
of every message always comes from the authenticated staff request, never a
client-supplied field, and ``is_staff_reply`` is set by the service.

Read serializers are staff-facing shapes carrying the author's display handle
and assignment context for the support console.
"""

from django.urls import reverse
from rest_framework import serializers

from apps.support.constants import (
    TICKET_MESSAGE_MAX_LENGTH,
    TICKET_SUBJECT_MAX_LENGTH,
)
from apps.support.models import Ticket, TicketMessage


class TicketCreateSerializer(serializers.Serializer):
    """Input for staff filing a ticket.

    ``order_id`` and ``return_request_id`` are optional references to link.
    ``message`` records the customer's complaint verbatim.
    """

    subject = serializers.CharField(max_length=TICKET_SUBJECT_MAX_LENGTH)
    category = serializers.ChoiceField(choices=Ticket.CATEGORY_CHOICES)
    message = serializers.CharField(max_length=TICKET_MESSAGE_MAX_LENGTH)
    order_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    return_request_id = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )


class TicketMessageCreateSerializer(serializers.Serializer):
    """Input for adding a message to a ticket.

    ``attachment`` is an optional file validated by content and size in the
    view before it reaches storage.
    """

    body = serializers.CharField(max_length=TICKET_MESSAGE_MAX_LENGTH)
    attachment = serializers.FileField(required=False, allow_null=True)


class TicketAssignSerializer(serializers.Serializer):
    """Input for assigning a ticket to a support agent."""

    agent_id = serializers.IntegerField(min_value=1)


class TicketStatusSerializer(serializers.Serializer):
    """Input for a staff status change on a ticket."""

    status = serializers.ChoiceField(choices=Ticket.STATUS_CHOICES)


class StaffTicketListQuerySerializer(serializers.Serializer):
    """Query-string filters for the staff ticket queue.

    ``status`` and ``category`` are restricted to their model choices, and
    ``assigned_to`` is a strictly numeric user id, so an invalid filter value
    yields a 400 rather than an unhandled query error.
    """

    status = serializers.ChoiceField(choices=Ticket.STATUS_CHOICES, required=False)
    category = serializers.ChoiceField(choices=Ticket.CATEGORY_CHOICES, required=False)
    assigned_to = serializers.IntegerField(required=False, min_value=1)


class StaffAuthorSerializer(serializers.Serializer):
    """Compact staff/customer identity for the support console — handle only."""

    id = serializers.IntegerField()
    username = serializers.CharField()


def _attachment_download_url(request, message):
    """Return the authenticated download URL for a message's attachment.

    The attachment is only reachable through the access-controlled download
    view; no raw storage path is ever surfaced. A request context without an
    authenticated caller (or a message with no attachment) yields None.

    Args:
        request: the serialization request context, if any.
        message (TicketMessage): the message being serialised.

    Returns:
        str | None: the download URL, or None.
    """
    if message.attachment:
        path = reverse("api:support:ticket-attachment-download", args=[message.pk])
        return request.build_absolute_uri(path) if request is not None else path
    return None


class StaffTicketMessageSerializer(serializers.ModelSerializer):
    """Staff-facing message shape with the author's display handle."""

    sender = serializers.SerializerMethodField()
    attachment = serializers.SerializerMethodField()

    class Meta:
        model = TicketMessage
        fields = ["id", "is_staff_reply", "sender", "body", "attachment", "created_at"]
        read_only_fields = fields

    def get_attachment(self, obj):
        """Return the authenticated download URL for the attachment, if any.

        Args:
            obj (TicketMessage): the message being serialised.

        Returns:
            str | None: the download URL when the message carries an
                attachment, else None.
        """
        return _attachment_download_url(self.context.get("request"), obj)

    def get_sender(self, obj):
        """Return the message author's display handle, if the account survives.

        Args:
            obj (TicketMessage): the message being serialised.

        Returns:
            dict | None: ``{"id": ..., "username": ...}`` or None when the
                author account was deleted.
        """
        if obj.sender is None:
            return None
        return {"id": obj.sender.id, "username": obj.sender.username}


class StaffTicketListSerializer(serializers.ModelSerializer):
    """Staff-facing list shape with author and assignee handles."""

    user = serializers.SerializerMethodField()
    assigned_to = serializers.SerializerMethodField()

    class Meta:
        model = Ticket
        fields = [
            "id",
            "subject",
            "category",
            "status",
            "order",
            "user",
            "assigned_to",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_user(self, obj):
        """Return the ticket opener's display handle, if any.

        Args:
            obj (Ticket): the ticket being serialised.

        Returns:
            dict | None: the opener's handle, or None.
        """
        if obj.user is None:
            return None
        return {"id": obj.user.id, "username": obj.user.username}

    def get_assigned_to(self, obj):
        """Return the assigned agent's display handle, if any.

        Args:
            obj (Ticket): the ticket being serialised.

        Returns:
            dict | None: the agent's handle, or None.
        """
        if obj.assigned_to is None:
            return None
        return {"id": obj.assigned_to.id, "username": obj.assigned_to.username}


class StaffTicketDetailSerializer(StaffTicketListSerializer):
    """Staff-facing detail shape with the full message thread."""

    messages = StaffTicketMessageSerializer(many=True, read_only=True)

    class Meta(StaffTicketListSerializer.Meta):
        fields = StaffTicketListSerializer.Meta.fields + ["messages"]
        read_only_fields = fields
