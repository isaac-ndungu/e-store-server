"""URL routes for the inquiries app."""

from django.urls import path

from apps.inquiries.views import (
    InquiryCreateView,
    InquiryListView,
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
]
