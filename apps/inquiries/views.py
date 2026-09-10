"""API views for the inquiries app.

``InquiryCreateView`` is the single write endpoint an anonymous visitor
touches: public, throttled, no auth. Staff queue views require a fulfilment
role and stay paginated.
"""

from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrSupport
from apps.core.api import service_error_to_400 as _service_error_to_400
from apps.inquiries.selectors import list_inquiries
from apps.inquiries.serializers import (
    InquiryCreateSerializer,
    InquiryDetailSerializer,
    InquiryStatusUpdateSerializer,
)
from apps.inquiries.services import record_inquiry, transition_inquiry


class InquiryCreateView(APIView):
    """Capture an anonymous WhatsApp/email hand-off (public).

    Fire-and-forget from the storefront: the visitor's WhatsApp/mail link
    opens regardless of this call's outcome, so the view does the minimum —
    validate, store, acknowledge — and never blocks on external work.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_write"

    @extend_schema(
        operation_id="inquiry_create",
        request=InquiryCreateSerializer,
        responses={201: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def post(self, request):
        """Store the hand-off snapshot.

        Args:
            request: the POST request with channel + cart snapshot.

        Returns:
            Response: ``201 Created`` with the stored inquiry.
        """
        input_serializer = InquiryCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        inquiry = record_inquiry(
            channel=data["channel"],
            cart_snapshot=data["cart_snapshot"],
            contact_hint=data.get("contact_hint", ""),
        )
        return Response(InquiryDetailSerializer(inquiry).data, status=201)


class InquiryListView(APIView):
    """List the staff follow-up queue, newest first (staff only)."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_read"

    @extend_schema(
        operation_id="inquiry_list",
        responses={200: InquiryDetailSerializer(many=True)},
        tags=["inquiries"],
    )
    def get(self, request):
        """Return paginated inquiries, optionally filtered.

        Args:
            request: the GET request with optional ``status``/``channel``.

        Returns:
            Response: the paginated queue.
        """
        queryset = list_inquiries(
            status=request.query_params.get("status") or None,
            channel=request.query_params.get("channel") or None,
        )
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(queryset, request)
        serializer = InquiryDetailSerializer(page, many=True)
        return Response(
            {
                "count": paginator.page.paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": serializer.data,
            }
        )


class InquiryStatusUpdateView(APIView):
    """Move an inquiry through the staff queue (staff only)."""

    permission_classes = [IsManagerOrSupport]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inquiry_read"

    def get_object(self, inquiry_id):
        """Return the inquiry or raise 404.

        Args:
            inquiry_id (int): the inquiry pk.

        Returns:
            Inquiry: the row.

        Raises:
            Http404: when missing.
        """
        from apps.inquiries.models import Inquiry

        try:
            return Inquiry.objects.select_related("converted_order").get(pk=inquiry_id)
        except Inquiry.DoesNotExist as exc:
            raise Http404 from exc

    @extend_schema(
        operation_id="inquiry_status_update",
        request=InquiryStatusUpdateSerializer,
        responses={200: InquiryDetailSerializer},
        tags=["inquiries"],
    )
    def post(self, request, inquiry_id):
        """Transition the inquiry status.

        Args:
            request: the POST request with ``to_status``.
            inquiry_id (int): the inquiry pk.

        Returns:
            Response: ``200 OK`` with the updated inquiry.
        """
        inquiry = self.get_object(inquiry_id)
        input_serializer = InquiryStatusUpdateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        updated = _service_error_to_400(transition_inquiry)(
            inquiry, input_serializer.validated_data["to_status"]
        )
        return Response(InquiryDetailSerializer(updated).data)
