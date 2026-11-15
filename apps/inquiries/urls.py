"""URL routes for the inquiries app."""

from django.urls import path

from apps.inquiries.views import (
    InquiryCreateView,
    InquiryListView,
    InquiryResendEmailView,
    InquiryStatusUpdateView,
)

urlpatterns = [
    path("inquiries/", InquiryCreateView.as_view(), name="inquiry-create"),
    path("inquiries/queue/", InquiryListView.as_view(), name="inquiry-queue"),
    path(
        "inquiries/<int:inquiry_id>/status/",
        InquiryStatusUpdateView.as_view(),
        name="inquiry-status-update",
    ),
    path(
        "inquiries/<int:inquiry_id>/resend-email/",
        InquiryResendEmailView.as_view(),
        name="inquiry-resend-email",
    ),
]
