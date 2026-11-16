"""Admin for the inquiry follow-up queue."""

from django.contrib import admin

from apps.inquiries.models import Inquiry


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    """Staff queue editor for captured hand-offs."""

    list_display = (
        "id",
        "channel",
        "status",
        "email_status",
        "contact_hint",
        "created_at",
    )
    list_filter = ("channel", "status", "email_status", "created_at")
    search_fields = ("contact_hint",)
    readonly_fields = (
        "email_status",
        "email_error",
        "email_provider_message_id",
        "email_attempts",
        "email_last_attempt_at",
        "email_sent_at",
        "created_at",
        "updated_at",
    )
