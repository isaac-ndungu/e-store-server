"""Django admin registration for the support app.

Tickets and chat sessions are handled through the staff API; these admin pages
are a read-and-repair safety net for operators. Messages are shown inline so a
thread is legible without leaving the parent record.
"""

from django.contrib import admin

from apps.support.models import ChatMessage, ChatSession, Ticket, TicketMessage


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


class ChatMessageInline(admin.TabularInline):
    """Inline view of a chat session's messages."""

    model = ChatMessage
    extra = 0
    readonly_fields = ("sender_type", "body", "sent_at")


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    """Admin page for browsing live-chat sessions."""

    list_display = (
        "id",
        "user",
        "guest_session_key",
        "assigned_agent",
        "started_at",
        "ended_at",
    )
    list_filter = ("ended_at",)
    search_fields = ("user__email", "user__username", "guest_session_key")
    raw_id_fields = ("user", "assigned_agent")
    inlines = [ChatMessageInline]
