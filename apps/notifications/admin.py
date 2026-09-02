from django.contrib import admin

from apps.notifications.models import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    """Admin page for outbound notification audit logs."""

    list_display = (
        "id",
        "channel",
        "purpose",
        "recipient",
        "status",
        "sent_by",
        "created_at",
    )
    list_filter = ("channel", "purpose", "status")
    search_fields = ("recipient", "message", "provider_message_id", "error_message")
    readonly_fields = (
        "channel",
        "purpose",
        "recipient",
        "message",
        "status",
        "provider_message_id",
        "provider_response",
        "error_message",
        "sent_by",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        """Prevent manual creation of log entries through the admin."""
        return False

    def has_change_permission(self, request, obj=None):
        """Logs are append-only — prevent in-place edits."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Logs are append-only — prevent deletion through the admin."""
        return False
