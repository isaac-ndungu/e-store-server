from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.notifications.models import NotificationLog
from apps.notifications.selectors import get_notification_logs
from apps.notifications.serializers import (
    NotificationLogSerializer,
    SendTestSMSSerializer,
)
from apps.notifications.services import send_test_sms


class SendTestSMSView(APIView):
    """Send a test SMS to a phone number and return the audit log.

    Staff-only (``IsAdminUser``).  Rate-limited with the dedicated
    ``notification_send`` scope so this endpoint cannot be abused as a
    free SMS relay.  The request body must contain ``recipient`` (E.164
    phone) and ``message`` (max 1600 chars).

    The endpoint is deliberately synchronous: the caller sees the provider
    outcome immediately so a Postman test can confirm success or failure
    in a single round trip.  A client-supplied ``Idempotency-Key`` header
    prevents a retried tap from sending a duplicate SMS (each SMS costs
    money).
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_send"

    def post(self, request):
        """Send the test SMS and return the resulting audit log.

        Args:
            request: the POST request carrying ``recipient`` and ``message``,
                plus an optional ``Idempotency-Key`` header.

        Returns:
            Response: ``201 Created`` with the ``NotificationLog`` payload on
                success, or ``400`` for validation errors.
        """
        serializer = SendTestSMSSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        idempotency_key = request.headers.get("Idempotency-Key")

        log = send_test_sms(
            recipient=serializer.validated_data["recipient"],
            message=serializer.validated_data["message"],
            sent_by=request.user,
            idempotency_key=idempotency_key,
        )

        return Response(
            NotificationLogSerializer(log, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class NotificationLogListView(generics.ListAPIView):
    """List notification logs with optional filtering.

    Staff-only (``IsAdminUser``).  Supports filtering by ``recipient``,
    ``channel``, ``status``, and ``purpose`` via query parameters, any
    combination of which is applied together.  Results are paginated.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = NotificationLogSerializer

    def get_queryset(self):
        """Return the notification-log queryset filtered by query params.

        Returns:
            QuerySet: ``NotificationLog`` rows matching the supplied filters.
        """
        params = self.request.query_params
        return get_notification_logs(
            recipient=params.get("recipient"),
            channel=params.get("channel"),
            status=params.get("status"),
            purpose=params.get("purpose"),
        )

    def get_serializer_context(self):
        """Add the request to the serializer context for error masking.

        Returns:
            dict: the serializer context including ``request``.
        """
        context = super().get_serializer_context()
        context["request"] = self.request
        return context


class NotificationLogDetailView(generics.RetrieveAPIView):
    """Retrieve a single notification log by id.

    Staff-only (``IsAdminUser``).  Supplements the Django admin with an
    API path for operational tooling that needs one log without a
    full-history scan.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = NotificationLogSerializer
    queryset = NotificationLog.objects.select_related("sent_by")


class FailedNotificationLogsView(generics.ListAPIView):
    """List notification logs with a ``failed`` status.

    Staff-only (``IsAdminUser``).  Useful for operational dashboards and
    retry tooling.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = NotificationLogSerializer

    def get_queryset(self):
        """Return all failed notification logs.

        Returns:
            QuerySet: failed ``NotificationLog`` rows.
        """
        return get_notification_logs(status="failed")


class DeliveryReportView(APIView):
    """Receive SMS delivery-report callbacks from Africa's Talking.

    Africa's Talking POSTs delivery reports to this endpoint when an SMS
    is delivered, expires, or fails.  The endpoint looks the log up by
    ``provider_message_id`` and updates its status to ``delivered`` or
    ``failed``.

    The endpoint is unauthenticated because the provider (not a user)
    calls it; the provider dashboard must be configured to POST here.
    Source-IP allowlisting should be applied at the proxy/CDN layer per
    the provider's documentation.  Update is idempotent: if the log is
    already in a terminal state the report is ignored.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        """Process a delivery-report payload.

        Args:
            request: the POST body containing the delivery report.

        Returns:
            Response: ``200 OK`` once the report is handled (or ignored).
        """
        provider_message_id = request.data.get("id") or request.data.get("messageId")
        if not provider_message_id:
            return Response(status=status.HTTP_200_OK)

        log = NotificationLog.objects.filter(
            provider_message_id=provider_message_id
        ).first()
        if log is None:
            return Response(status=status.HTTP_200_OK)

        state = request.data.get("status")
        if log.status in ("delivered", "failed"):
            return Response(status=status.HTTP_200_OK)

        new_status = "failed" if (state and state.lower() == "failed") else "delivered"
        log.update_status(new_status, provider_response=request.data)
        return Response(status=status.HTTP_200_OK)
