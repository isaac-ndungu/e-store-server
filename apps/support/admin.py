"""Django admin registration for the support app.

Tickets are handled through the staff API; these admin pages are a
read-and-repair safety net for operators. Messages are shown inline so a
thread is legible without leaving the parent record.
"""

from django.contrib import admin

from apps.support.models import Ticket, TicketMessage


class TicketMessageInline(admin.TabularInline):
    """Inline view of a ticket's messages."""

    model = TicketMessage
    extra = 0
    readonly_fields = ("sender", "is_staff_reply", "body", "attachment", "created_at")


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    """Admin page for browsing support tickets."""

    list_display = (
        "id",
        "subject",
        "category",
        "status",
        "user",
        "assigned_to",
        "created_at",
    )
    list_filter = ("status", "category")
    search_fields = ("subject", "user__email", "user__username")
    raw_id_fields = ("user", "order", "assigned_to")
    inlines = [TicketMessageInline]
