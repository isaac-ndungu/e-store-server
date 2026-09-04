"""API views for the payments app.

The payment views are split by caller type:

- ``MpesaSTKCallbackView`` and ``MpesaB2CCallbackView`` — public, unauthenticated
  endpoints that Safaricom's Daraja API calls back on.  These validate the source
  IP against an allowlist before processing.  No authentication is applied because
  Safaricom does not carry a JWT; security relies on network-level source
  restriction plus idempotent processing.

- ``MpesaTransactionStatusView`` — authenticated, returns the caller's own M-Pesa
  transaction status so the storefront can poll for confirmation after initiating
  an order.

All mutations go through the payments service layer; the database is never
written to directly in a view.
"""

import hmac
import logging

from decouple import config
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.payments.services import handle_b2c_callback, handle_stk_callback

logger = logging.getLogger(__name__)

_MPESA_CALLBACK_IPS = config(
    "MPESA_CALLBACK_IPS",
    default="196.201.214.200,196.201.214.201,196.201.214.202,196.201.214.203",
    cast=lambda v: [ip.strip() for ip in v.split(",") if ip.strip()],
)

# Shared secret appended to the Daraja callback URLs (as a query parameter).
# Safaricom echoes back whatever URL it was configured with, so a request that
# reaches the endpoint without a valid token could not have come from the
# configured callback URL.  This is an independent layer of trust on top of the
# source-IP allowlist: if the IP ranges change upstream or a request bypasses
# the network check, the token still gates processing.  Empty disables the
# check (local dev).
_MPESA_CALLBACK_SECRET = config("MPESA_CALLBACK_SECRET", default="")

_CALLBACK_TOKEN_PARAM = "token"


def _callback_token_matches(request):
    """Return whether the request carries the shared callback secret.

    Args:
        request: the incoming HTTP request.

    Returns:
        bool: True when no secret is configured or the token query parameter
            matches (constant-time comparison).
    """
    provided = request.query_params.get(_CALLBACK_TOKEN_PARAM, "")
    if not _MPESA_CALLBACK_SECRET:
        return True
    return hmac.compare_digest(provided, _MPESA_CALLBACK_SECRET)


def _client_ip(request):
    """Return the client's IP address, respecting X-Forwarded-For.

    In production behind a reverse proxy the real client IP is in
    ``X-Forwarded-For``.  The first entry in the chain is the original client.

    Args:
        request: the incoming HTTP request.

    Returns:
        str: the client IP address.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _is_callback_ip_allowed(request):
    """Return whether the request's source IP is in the Daraja callback allowlist.

    An empty allowlist disables the check (useful in local dev where callbacks
    come from localhost).  A deny is logged at warning level so operators can
    tune the allowlist.

    Args:
        request: the incoming HTTP request.

    Returns:
        bool: True when the IP is allowed or the allowlist is empty.
    """
    if not _MPESA_CALLBACK_IPS:
        return True
    ip = _client_ip(request)
    allowed = ip in _MPESA_CALLBACK_IPS
    if not allowed:
        logger.warning("M-Pesa callback rejected from unauthorized IP %s", ip)
    return allowed


@method_decorator(csrf_exempt, name="dispatch")
class MpesaSTKCallbackView(APIView):
    """Receive the M-Pesa STK Push callback from Safaricom.

    This endpoint is public and unauthenticated — Safaricom does not carry a
    JWT.  Security is enforced by:
        1. Source IP validation against the Daraja callback IP allowlist.
        2. Idempotent processing via ``checkout_request_id`` lookups in the
           service layer — a duplicate callback is silently no-oped.
        3. CSRF exemption since this is a webhook, not a browser form.

    The callback body is ``{"Body": {"StkCallback": {...}}}``.  On success
    the order is confirmed and stock is fulfilled.  On failure the order is
    cancelled and stock is released.  Both paths go through the order service
    so the status machine and audit trail are never bypassed.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mpesa_callback"

    def post(self, request):
        """Process the STK Push callback.

        Args:
            request: the POST request carrying the Daraja callback body.

        Returns:
            JsonResponse: ``{"ResultCode": 0, "ResultDesc": "success"}``
                on acceptance (the response Daraja expects), or ``403`` when
                the source IP is not allowed.
        """
        if not _is_callback_ip_allowed(request):
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Unauthorized IP"},
                status=403,
            )
        if not _callback_token_matches(request):
            logger.warning("STK callback rejected: invalid or missing token")
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Unauthorized"},
                status=401,
            )

        body = request.data
        logger.info("STK callback received from IP %s", _client_ip(request))

        try:
            result = handle_stk_callback(body)
        except Exception:
            logger.exception("Unhandled error processing STK callback")
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Internal processing error"},
                status=500,
            )

        if result is None:
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Invalid callback payload"},
                status=400,
            )

        return JsonResponse({"ResultCode": 0, "ResultDesc": "success"})


@method_decorator(csrf_exempt, name="dispatch")
class MpesaB2CCallbackView(APIView):
    """Receive the M-Pesa B2C callback from Safaricom.

    Same security posture as the STK callback: IP validation + idempotent
    processing via ``conversation_id`` lookups.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mpesa_callback"

    def post(self, request):
        """Process the B2C callback.

        Args:
            request: the POST request carrying the Daraja B2C callback body.

        Returns:
            JsonResponse: ``{"ResultCode": 0, "ResultDesc": "success"}``
                on acceptance, or ``403`` when the source IP is not allowed.
        """
        if not _is_callback_ip_allowed(request):
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Unauthorized IP"},
                status=403,
            )
        if not _callback_token_matches(request):
            logger.warning("B2C callback rejected: invalid or missing token")
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Unauthorized"},
                status=401,
            )

        body = request.data
        logger.info("B2C callback received from IP %s", _client_ip(request))

        try:
            result = handle_b2c_callback(body)
        except Exception:
            logger.exception("Unhandled error processing B2C callback")
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Internal processing error"},
                status=500,
            )

        if result is None:
            return JsonResponse(
                {"ResultCode": 1, "ResultDesc": "Invalid callback payload"},
                status=400,
            )

        return JsonResponse({"ResultCode": 0, "ResultDesc": "success"})


class MpesaTransactionStatusView(APIView):
    """Return the M-Pesa transaction status for a caller's order.

    Authenticated: the caller must own the order.  Used by the storefront to
    poll for confirmation after an order with ``payment_method='mpesa'`` is
    placed — the STK Push triggers a prompt on the customer's phone, and the
    storefront polls this endpoint until the status moves from ``pending`` to
    ``success`` or a terminal failure.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_read"

    def get(self, request, transaction_id):
        """Return the M-Pesa transaction status.

        Args:
            request: the GET request.
            transaction_id (int): the MpesaTransaction primary key.

        Returns:
            Response: the transaction detail, ``403`` if the caller does not
                own the order, or ``404`` if the transaction does not exist.
        """
        from apps.payments.selectors import get_mpesa_transaction_for_user

        transaction_record = get_mpesa_transaction_for_user(
            request.user, transaction_id
        )
        if transaction_record is None:
            raise PermissionDenied("Transaction not found or access denied.")

        from apps.payments.serializers import MpesaTransactionSerializer

        serializer = MpesaTransactionSerializer(transaction_record)
        return Response(serializer.data)
