"""URL routing for the support endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Customer ticket routes sit
under ``support/tickets/`` and are login-only; customer chat routes under
``support/chat-sessions/`` serve logged-in customers and guests alike. Staff
routes under ``support/admin/`` are reachable only by manager/support tokens.
"""

from django.urls import path

from apps.support.views import (
    ChatMessageCreateView,
    ChatSessionCreateView,
    ChatSessionDetailView,
    ChatSessionEndView,
    StaffChatAssignView,
    StaffChatMessageCreateView,
    StaffChatSessionDetailView,
    StaffChatSessionListView,
    StaffTicketAssignView,
    StaffTicketDetailView,
    StaffTicketListView,
    StaffTicketReplyView,
    StaffTicketStatusView,
    TicketAttachmentDownloadView,
    TicketDetailView,
    TicketListCreateView,
    TicketMessageCreateView,
)

urlpatterns = [
    path(
        "support/tickets/",
        TicketListCreateView.as_view(),
        name="ticket-list-create",
    ),
    path(
        "support/tickets/<int:ticket_id>/",
        TicketDetailView.as_view(),
        name="ticket-detail",
    ),
    path(
        "support/tickets/<int:ticket_id>/messages/",
        TicketMessageCreateView.as_view(),
        name="ticket-message-create",
    ),
    path(
        "support/attachments/<int:message_id>/download/",
        TicketAttachmentDownloadView.as_view(),
        name="ticket-attachment-download",
    ),
    path(
        "support/chat-sessions/",
        ChatSessionCreateView.as_view(),
        name="chat-session-create",
    ),
    path(
        "support/chat-sessions/<int:session_id>/",
        ChatSessionDetailView.as_view(),
        name="chat-session-detail",
    ),
    path(
        "support/chat-sessions/<int:session_id>/messages/",
        ChatMessageCreateView.as_view(),
        name="chat-message-create",
    ),
    path(
        "support/chat-sessions/<int:session_id>/end/",
        ChatSessionEndView.as_view(),
        name="chat-session-end",
    ),
    path(
        "support/admin/tickets/",
        StaffTicketListView.as_view(),
        name="staff-ticket-list",
    ),
    path(
        "support/admin/tickets/<int:ticket_id>/",
        StaffTicketDetailView.as_view(),
        name="staff-ticket-detail",
    ),
    path(
        "support/admin/tickets/<int:ticket_id>/reply/",
        StaffTicketReplyView.as_view(),
        name="staff-ticket-reply",
    ),
    path(
        "support/admin/tickets/<int:ticket_id>/assign/",
        StaffTicketAssignView.as_view(),
        name="staff-ticket-assign",
    ),
    path(
        "support/admin/tickets/<int:ticket_id>/status/",
        StaffTicketStatusView.as_view(),
        name="staff-ticket-status",
    ),
    path(
        "support/admin/chat-sessions/",
        StaffChatSessionListView.as_view(),
        name="staff-chat-list",
    ),
    path(
        "support/admin/chat-sessions/<int:session_id>/",
        StaffChatSessionDetailView.as_view(),
        name="staff-chat-detail",
    ),
    path(
        "support/admin/chat-sessions/<int:session_id>/messages/",
        StaffChatMessageCreateView.as_view(),
        name="staff-chat-message-create",
    ),
    path(
        "support/admin/chat-sessions/<int:session_id>/assign/",
        StaffChatAssignView.as_view(),
        name="staff-chat-assign",
    ),
]
