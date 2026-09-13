"""URL routing for the support endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Ticket routes sit
under ``support/tickets/`` and are staff-only internal tracking. Staff
routes under ``support/admin/`` are reachable only by manager/support
tokens.
"""

from django.urls import path

from apps.support.views import (
    StaffTicketAssignView,
    StaffTicketDetailView,
    StaffTicketListView,
    StaffTicketReplyView,
    StaffTicketStatusView,
    TicketAttachmentDownloadView,
    TicketListCreateView,
)

urlpatterns = [
    path(
        "support/tickets/",
        TicketListCreateView.as_view(),
        name="ticket-list-create",
    ),
    path(
        "support/attachments/<int:message_id>/download/",
        TicketAttachmentDownloadView.as_view(),
        name="ticket-attachment-download",
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
]
