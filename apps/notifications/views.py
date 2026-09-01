"""API views for the notifications app.

Implements the internal test-send endpoint and notification-log retrieval.
The test-send view is staff-only so only managers/support can trigger a
real SMS.  Log queries are also staff-only so operational dashboards can
read audit data without exposing it to customers.
"""

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.notifications.models import NotificationLog
from apps.notifications.selectors import (
    get_failed_notification_logs,
    get_notification_logs_by_purpose,
    get_notification_logs_for_recipient,
)
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
    in a single round trip.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_send"

    def post(self, request):
        """Send the test SMS and return the resulting audit log.

        Args:
            request: the POST request carrying ``recipient`` and ``message``.

        Returns:
            Response: ``201 Created`` with the ``NotificationLog`` payload on
                success, or ``400`` for validation errors.
        """
        serializer = SendTestSMSSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        log = send_test_sms(
            recipient=serializer.validated_data["recipient"],
            message=serializer.validated_data["message"],
            sent_by=request.user,
        )

        return Response(
            NotificationLogSerializer(log).data,
            status=status.HTTP_201_CREATED,
        )


class NotificationLogListView(generics.ListAPIView):
    """List notification logs with optional filtering.

    Staff-only (``IsAdminUser``).  Supports filtering by ``recipient``,
    ``channel``, ``status``, and ``purpose`` via query parameters.  Results
    are paginated using the project default.
    """

    permission_classes = [permissions.IsAdminUser]
    serializer_class = NotificationLogSerializer
    filterset_fields = ["recipient", "channel", "status", "purpose"]
    ordering_fields = ["created_at", "status", "purpose"]
    ordering = ["-created_at"]

    def get_queryset(self):
        """Return the full notification log queryset.

        When a ``recipient`` query parameter is provided, delegates to the
        selector for a recipient-scoped query; otherwise returns all logs.

        Returns:
            QuerySet: ``NotificationLog`` rows ordered by ``-created_at``.
        """
        recipient = self.request.query_params.get("recipient")
        channel = self.request.query_params.get("channel")
        purpose = self.request.query_params.get("purpose")

        if purpose:
            return get_notification_logs_by_purpose(purpose)
        if recipient:
            return get_notification_logs_for_recipient(recipient, channel=channel)
        qs = NotificationLog.objects.all()
        if channel:
            qs = qs.filter(channel=channel)
        return qs.select_related("sent_by")


class FailedNotificationLogsView(generics.ListAPIView):
    """List notification logs with a ``failed`` status.

    Staff-only (``IsAdminUser``).  Useful for operational dashboards and
    retry tooling.
    """

    permission_classes = [permissions.IsAdminUser]
    serializer_class = NotificationLogSerializer
    ordering = ["-created_at"]

    def get_queryset(self):
        """Return all failed notification logs.

        Returns:
            QuerySet: failed ``NotificationLog`` rows.
        """
        return get_failed_notification_logs()
