"""SMS-sending abstraction for the notifications app.

All outbound SMS traffic flows through ``send_sms()``, which delegates to
the configured provider (Africa's Talking by default). The function creates
a ``NotificationLog`` before the provider call, updates it on success or
failure, and returns the log so callers can inspect the outcome without
hitting the provider again.

The provider is selected via the ``SMS_PROVIDER`` environment variable so
the integration can be swapped without code changes. Currently supported:

- ``africastalking`` (default) — Africa's Talking REST API via the
  official Python SDK.

Tests mock ``_send_via_provider`` so no real network calls are made.
"""

import logging

from decouple import config

from apps.notifications.models import NotificationLog

logger = logging.getLogger(__name__)

# SMS provider configuration pulled from the environment.  Individual
# provider modules read their own keys; this module only reads the
# provider selector and the shared sender ID.
SMS_PROVIDER = config("SMS_PROVIDER", default="africastalking")
SMS_SENDER_ID = config("SMS_SENDER_ID", default="")


def _send_via_provider(recipient, message, sender_id=""):
    """Dispatch an SMS through the configured provider and return its response.

    This is the single external-network call in the SMS path.  It is
    deliberately kept as a pure function (no DB side effects) so tests
    can mock it cleanly.

    Args:
        recipient (str): E.164 phone number.
        message (str): the SMS body text.
        sender_id (str): alphanumeric sender ID, if the provider supports it.

    Returns:
        dict: a normalised ``{"success": bool, "message_id": str,
        "response": dict, "error": str}`` payload.

    Raises:
        ImportError: if the provider SDK is not installed.
    """
    if SMS_PROVIDER == "africastalking":
        return _send_africastalking(recipient, message, sender_id)

    raise ValueError(f"Unsupported SMS provider: {SMS_PROVIDER}")


def _send_africastalking(recipient, message, sender_id=""):
    """Send an SMS via Africa's Talking and normalise the response.

    Args:
        recipient (str): E.164 phone number.
        message (str): the SMS body text.
        sender_id (str): alphanumeric sender ID.

    Returns:
        dict: normalised response with ``success``, ``message_id``,
        ``response``, and ``error`` keys.
    """
    try:
        import africastalking
    except ImportError as exc:
        raise ImportError(
            "africastalking SDK is not installed. "
            "Install it with: pip install africastalking"
        ) from exc

    username = config("AT_USERNAME", default="sandbox")
    api_key = config("AT_API_KEY", default="")

    africastalking.initialize(username=username, api_key=api_key)
    sms = africastalking.SMS

    try:
        result = sms.send(message, [recipient], sender_id=sender_id)
        if result and isinstance(result, dict):
            recipients = result.get("SMSMessageData", {}).get("Recipients", [])
            if recipients:
                first = recipients[0]
                if first.get("status") == "Success":
                    return {
                        "success": True,
                        "message_id": first.get("messageId", ""),
                        "response": result,
                        "error": "",
                    }
                return {
                    "success": False,
                    "message_id": "",
                    "response": result,
                    "error": first.get("status", "Unknown error"),
                }
        return {
            "success": False,
            "message_id": "",
            "response": result or {},
            "error": "Empty or malformed provider response",
        }
    except Exception as exc:
        logger.exception("Africa's Talking SMS send failed for %s", recipient)
        return {
            "success": False,
            "message_id": "",
            "response": {},
            "error": str(exc),
        }


def send_sms(
    recipient,
    message,
    purpose="transactional",
    sent_by=None,
    sender_id=None,
):
    """Send an SMS and persist the audit log.

    Creates a ``NotificationLog`` in ``pending`` status, dispatches via the
    provider, and updates the log to ``sent`` or ``failed``.  Always
    returns the log instance so callers can inspect the outcome.

    Args:
        recipient (str): E.164 phone number to send to.
        message (str): the SMS body text.
        purpose (str): one of the ``NotificationLog.PURPOSE_CHOICES`` values.
        sent_by (User | None): the staff user who triggered this send, if any.
        sender_id (str | None): override sender ID; falls back to the
            ``SMS_SENDER_ID`` environment variable.

    Returns:
        NotificationLog: the persisted audit record for this send.
    """
    effective_sender_id = sender_id if sender_id is not None else SMS_SENDER_ID

    log = NotificationLog.objects.create(
        channel="sms",
        purpose=purpose,
        recipient=recipient,
        message=message,
        sent_by=sent_by,
    )

    result = _send_via_provider(recipient, message, effective_sender_id)

    if result["success"]:
        log.update_status(
            "sent",
            provider_message_id=result["message_id"],
            provider_response=result["response"],
        )
    else:
        log.update_status(
            "failed",
            provider_response=result["response"],
            error_message=result["error"],
        )

    return log


def send_otp_sms(recipient, otp_code, sent_by=None):
    """Send a one-time password SMS.

    Composes a standard OTP message and delegates to ``send_sms()``.
    The OTP value is never logged in plaintext in the ``message`` field
    of ``NotificationLog`` for security — only the provider sees the
    full text.

    Args:
        recipient (str): E.164 phone number.
        otp_code (str): the numeric OTP to embed in the message.
        sent_by (User | None): staff user who triggered the send, if any.

    Returns:
        NotificationLog: the audit record for this OTP send.
    """
    message = (
        f"Your verification code is {otp_code}. "
        "It expires in 10 minutes. Do not share this code."
    )
    return send_sms(recipient, message, purpose="otp", sent_by=sent_by)


def send_test_sms(recipient, message, sent_by=None):
    """Send a test SMS from the internal test-send endpoint.

    Identical to ``send_sms()`` but uses the ``test`` purpose so it is
    easy to distinguish from production traffic in the audit log.

    Args:
        recipient (str): E.164 phone number.
        message (str): the SMS body text.
        sent_by (User | None): the staff user who triggered the test.

    Returns:
        NotificationLog: the audit record for this test send.
    """
    return send_sms(recipient, message, purpose="test", sent_by=sent_by)
