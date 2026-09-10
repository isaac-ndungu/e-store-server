import hmac
import logging
import re

from decouple import config
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.http import client_ip
from apps.notifications.models import NotificationLog
from apps.notifications.selectors import get_notification_logs
from apps.notifications.serializers import (
    NotificationLogSerializer,
    SendTestSMSSerializer,
)
from apps.notifications.services import send_test_sms

logger = logging.getLogger(__name__)

_CALLBACK_IPS = config(
    "NOTIFICATIONS_CALLBACK_IPS",
    default="",
    cast=lambda v: [ip.strip() for ip in v.split(",") if ip.strip()],
)

_CALLBACK_SECRET = config("NOTIFICATIONS_CALLBACK_SECRET", default="")
_CALLBACK_TOKEN_PARAM = "token"

# Matches an E.164 or a whitespace-padded local phone number in provider
# payloads so the stored audit copy never holds a full unmasked number.
_PHONE_RE = re.compile(r"(\+?[0-9][0-9\s\-]{7,})")


def _is_callback_ip_allowed(request):
    """Return whether the request's source IP is in the callback allowlist.

    An empty allowlist disables the check (local dev friendly). A deny is
    logged at warning level so operators can tune the allowlist.

    Args:
        request: the incoming HTTP request.

    Returns:
        bool: True when the IP is allowed or the allowlist is empty.
    """
    if not _CALLBACK_IPS:
        return True
    ip = client_ip(request)
    allowed = ip in _CALLBACK_IPS
    if not allowed:
        logger.warning("Delivery-report callback rejected from unauthorized IP %s", ip)
    return allowed


def _callback_token_matches(request):
    """Return whether the request carries the shared callback secret.

    Args:
        request: the incoming HTTP request.

    Returns:
        bool: True when no secret is configured or the token query parameter
            matches (constant-time comparison).
    """
    provided = request.query_params.get(_CALLBACK_TOKEN_PARAM, "")
    if not _CALLBACK_SECRET:
        return True
    return hmac.compare_digest(provided, _CALLBACK_SECRET)


def _mask_phone_numbers(payload):
    """Return a copy of the payload with phone numbers masked.

    Args:
        payload (dict): the raw provider payload.

    Returns:
        dict: the payload with any phone-like digit runs replaced by
            ``+2547*****31``-style placeholders.
    """
    if not isinstance(payload, dict):
        return payload

    def _mask(value):
        if not isinstance(value, str):
            return value
        return _PHONE_RE.sub(
            lambda m: m.group(0)[:3]
            + "*" * max(1, len(m.group(0)) - 5)
            + m.group(0)[-2:],
            value,
        )

    return {key: _mask(value) for key, value in payload.items()}


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

    @extend_schema(
        operation_id="admin_test_sms_send",
        request=SendTestSMSSerializer,
        responses={201: NotificationLogSerializer},
    )
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


@method_decorator(csrf_exempt, name="dispatch")
class DeliveryReportView(APIView):
    """Receive SMS delivery-report callbacks from Africa's Talking.

    Africa's Talking POSTs delivery reports to this endpoint when an SMS
    is delivered, expires, or fails. The endpoint looks the log up by
    ``provider_message_id`` and updates its status to ``delivered`` or
    ``failed``.

    The endpoint is public because the provider (not a user) calls it, and
    its trust is enforced by:
        1. Source-IP validation against the ``NOTIFICATIONS_CALLBACK_IPS``
           allowlist.
        2. A shared ``token`` query parameter compared in constant time,
           configured via ``NOTIFICATIONS_CALLBACK_SECRET``.
        3. CSRF exemption, since this is a provider webhook, not a browser
           form.
    Update is idempotent: if the log is already in a terminal state the
    report is ignored. Stored provider payloads are masked so a phone number
    is never persisted in full.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notifications_callback"

    @extend_schema(
        operation_id="sms_delivery_report_callback",
        request=dict,
        responses={200: None},
    )
    def post(self, request):
        """Process a delivery-report payload.

        Args:
            request: the POST body containing the delivery report.

        Returns:
            Response: ``200 OK`` once the report is handled (or ignored), or
                ``403``/``401`` when the allowlist or token checks fail.
        """
        if not _is_callback_ip_allowed(request):
            return Response(status=status.HTTP_403_FORBIDDEN)
        if not _callback_token_matches(request):
            logger.warning(
                "Delivery-report callback rejected: invalid or missing token"
            )
            return Response(status=status.HTTP_401_UNAUTHORIZED)

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
        log.update_status(
            new_status,
            provider_response=_mask_phone_numbers(request.data),
        )
        return Response(status=status.HTTP_200_OK)
