"""Admin for the inquiry follow-up queue."""

from django.contrib import admin

from apps.inquiries.models import Inquiry


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    """Staff queue editor for captured hand-offs."""

    list_display = ("id", "channel", "status", "contact_hint", "created_at")
    list_filter = ("channel", "status", "created_at")
    search_fields = ("contact_hint",)
    readonly_fields = ("created_at", "updated_at")
