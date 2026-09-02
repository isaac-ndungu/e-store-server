import logging

from decouple import config
from django.core.cache import cache
from rest_framework import serializers

from apps.accounts.services import validate_phone_number
from apps.notifications.models import NotificationLog

logger = logging.getLogger(__name__)

# SMS provider configuration pulled from the environment.  Individual
# provider modules read their own keys; this module only reads the
# provider selector and the shared sender ID.
SMS_PROVIDER = config("SMS_PROVIDER", default="africastalking")
SMS_SENDER_ID = config("SMS_SENDER_ID", default="")

# Per-recipient outbound rate limit: max sends within the sliding window.
_SEND_RATE_LIMIT = 5
_SEND_RATE_WINDOW = 60  # seconds


def _provider_exceptions():
    """Return the tuple of provider SDK exceptions to treat as send failures.

    Africa's Talking raises its own exception type for network and auth
    failures during a send.  This is a real, retryable failure mode, so it
    must be caught and surfaced as a ``failed`` result — while a bare
    ``Exception`` continues to be avoided so genuine programming errors
    still break loudly.

    Returns:
        tuple: the exception types to catch from the provider call.
    """
    try:
        from africastalking import Service

        return (Service.AfricasTalkingException,)
    except ImportError:
        return ()


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
                        "segments": result.get("SMSMessageData", {}).get("NumSegments"),
                        "error": "",
                    }
                return {
                    "success": False,
                    "message_id": "",
                    "response": result,
                    "segments": result.get("SMSMessageData", {}).get("NumSegments"),
                    "error": first.get("status", "Unknown error"),
                }
        return {
            "success": False,
            "message_id": "",
            "response": result or {},
            "segments": None,
            "error": "Empty or malformed provider response",
        }
    except (OSError, _provider_exceptions()) as exc:
        logger.exception("Africa's Talking SMS send failed for %s", recipient)
        return {
            "success": False,
            "message_id": "",
            "response": {},
            "segments": None,
            "error": str(exc),
        }


def _check_send_rate_limit(recipient):
    """Reject the send if the per-recipient rate limit is exceeded.

    Uses a sliding-window counter in Django's cache backend so no DB
    write is needed.  The limit is ``_SEND_RATE_LIMIT`` sends per
    ``_SEND_RATE_WINDOW`` seconds per phone number.

    Args:
        recipient (str): E.164 phone number.

    Raises:
        serializers.ValidationError: if the rate limit is exceeded.
    """
    cache_key = f"sms_rate:{recipient}"
    count = cache.get(cache_key, 0)
    if count >= _SEND_RATE_LIMIT:
        raise serializers.ValidationError(
            f"Too many SMS sends to {recipient}. " "Please wait a moment and try again."
        )
    cache.set(cache_key, count + 1, timeout=_SEND_RATE_WINDOW)


def send_sms(
    recipient,
    message,
    purpose="transactional",
    sent_by=None,
    sender_id=None,
    log_message=None,
    idempotency_key=None,
):
    """Send an SMS and persist the audit log.

    Creates a ``NotificationLog`` in ``pending`` status, dispatches via the
    provider, and updates the log to ``sent`` or ``failed``.  Always
    returns the log instance so callers can inspect the outcome.

    When ``idempotency_key`` is provided and a log with that key already
    exists, the existing log is returned immediately without sending a
    duplicate SMS.

    Args:
        recipient (str): E.164 phone number to send to.
        message (str): the SMS body text sent to the provider.
        purpose (str): one of the ``NotificationLog.PURPOSE_CHOICES`` values.
        sent_by (User | None): the staff user who triggered this send, if any.
        sender_id (str | None): override sender ID; falls back to the
            ``SMS_SENDER_ID`` environment variable.
        log_message (str | None): text to persist in the audit log; defaults
            to ``message``. Pass a redacted value for secret-bearing sends.
        idempotency_key (str | None): client-supplied dedup key. When
            provided, a prior send with the same key returns the cached
            result instead of sending a duplicate.

    Returns:
        NotificationLog: the persisted audit record for this send.

    Raises:
        serializers.ValidationError: if ``recipient`` is not a valid E.164
            phone number or the per-recipient rate limit is exceeded.
    """
    if idempotency_key:
        existing = NotificationLog.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing

    recipient = validate_phone_number(recipient)
    _check_send_rate_limit(recipient)

    if log_message is None:
        log_message = message
    effective_sender_id = sender_id if sender_id is not None else SMS_SENDER_ID

    log = NotificationLog.objects.create(
        channel="sms",
        purpose=purpose,
        recipient=recipient,
        message=log_message,
        sent_by=sent_by,
        idempotency_key=idempotency_key,
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

    if result.get("segments") is not None:
        log.segments = int(result["segments"])
        log.save(update_fields=["segments"])
    else:
        segments = (
            result.get("response", {}).get("SMSMessageData", {}).get("NumSegments")
        )
        if segments is not None:
            log.segments = int(segments)
            log.save(update_fields=["segments"])

    return log


def send_otp_sms(recipient, otp_code, sent_by=None):
    """Send a one-time password SMS.

    Composes a standard OTP message and delegates to ``send_sms()``. The
    provider receives the full text containing the OTP, but the value
    persisted to ``NotificationLog.message`` is masked (``******``) so a
    live OTP never lands in the database or the audit-log endpoints. The
    masked placeholder is not a usable credential, so a DB read or a future
    report over ``NotificationLog`` cannot leak the code.

    Args:
        recipient (str): E.164 phone number.
        otp_code (str): the numeric OTP to embed in the message.
        sent_by (User | None): staff user who triggered the send, if any.

    Returns:
        NotificationLog: the audit record for this OTP send.
    """
    from apps.notifications.notification_templates import render_message

    message = render_message("sms", "otp", code=otp_code, expiry_minutes=10)
    masked_message = message.replace(otp_code, "******")
    return send_sms(
        recipient, message, purpose="otp", sent_by=sent_by, log_message=masked_message
    )


def send_email(
    recipient,
    subject,
    body,
    purpose="transactional",
    sent_by=None,
    html_body=None,
):
    """Send an email and persist the audit log.

    Creates a ``NotificationLog`` with ``channel="email"``, delegates to
    Django's ``send_mail``, and updates the log to ``sent`` or ``failed``.

    Args:
        recipient (str): email address.
        subject (str): email subject line.
        body (str): plaintext email body.
        purpose (str): one of the ``NotificationLog.PURPOSE_CHOICES`` values.
        sent_by (User | None): staff user who triggered the send, if any.
        html_body (str | None): optional HTML body for multipart emails.

    Returns:
        NotificationLog: the audit record for this send.
    """
    from django.conf import settings
    from django.core.mail import send_mail as django_send_mail

    log = NotificationLog.objects.create(
        channel="email",
        purpose=purpose,
        recipient=recipient,
        message=f"[Subject] {subject}\n\n{body[:500]}",
        sent_by=sent_by,
    )

    try:
        result = django_send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [recipient],
            html_message=html_body,
        )
        log.update_status(
            "sent",
            provider_response={"result": result},
        )
    except OSError as exc:
        logger.exception("Email send failed for %s", recipient)
        log.update_status(
            "failed",
            error_message=str(exc),
        )

    return log


def send_notification(
    channel,
    purpose,
    recipient,
    *,
    template_key=None,
    context=None,
    message=None,
    sent_by=None,
    idempotency_key=None,
    **kwargs,
):
    """Unified entry point for sending notifications across channels.

    Routes to ``send_sms()`` or ``send_email()`` based on ``channel``.
    If ``template_key`` is provided, the message body is rendered from the
    template registry.  Otherwise ``message`` (for SMS) or ``body`` (for
    email, via ``kwargs["body"]``) must be supplied directly.

    Args:
        channel (str): ``"sms"`` or ``"email"``.
        purpose (str): notification purpose.
        recipient (str): phone number (E.164) or email address.
        template_key (str | None): key into the template registry.
        context (dict | None): template rendering context.
        message (str | None): raw message body (SMS).
        sent_by (User | None): staff user who triggered the send.
        idempotency_key (str | None): dedup key for SMS sends.
        **kwargs: additional channel-specific arguments
            (``subject``, ``body``, ``html_body`` for email).

    Returns:
        NotificationLog: the audit record for this send.

    Raises:
        ValueError: if channel is unknown or message cannot be resolved.
    """
    if template_key and context is not None:
        from apps.notifications.notification_templates import render_message

        message = render_message(channel, template_key, **context)

    if channel == "sms":
        if not message:
            raise ValueError("SMS notification requires a message body.")
        return send_sms(
            recipient,
            message,
            purpose=purpose,
            sent_by=sent_by,
            idempotency_key=idempotency_key,
        )
    elif channel == "email":
        subject = kwargs.get("subject", "")
        body = kwargs.get("body", "")
        html_body = kwargs.get("html_body")
        if not body and message:
            body = message
        return send_email(
            recipient,
            subject,
            body,
            purpose=purpose,
            sent_by=sent_by,
            html_body=html_body,
        )
    else:
        raise ValueError(f"Unsupported notification channel: {channel}")


def send_test_sms(recipient, message, sent_by=None, idempotency_key=None):
    """Send a test SMS from the internal test-send endpoint.

    Identical to ``send_sms()`` but uses the ``test`` purpose so it is
    easy to distinguish from production traffic in the audit log.

    Args:
        recipient (str): E.164 phone number.
        message (str): the SMS body text.
        sent_by (User | None): the staff user who triggered the test.
        idempotency_key (str | None): dedup key for the send.

    Returns:
        NotificationLog: the audit record for this test send.
    """
    return send_sms(
        recipient,
        message,
        purpose="test",
        sent_by=sent_by,
        idempotency_key=idempotency_key,
    )


def notify_low_stock(variant, warehouse, quantity, threshold):
    """Notify staff users when a variant's stock runs low.

    Composes a message from the low-stock template and sends an SMS to
    every staff user with a phone number.  Intended to be called by the
    inventory layer whenever a stock move drops a variant below its
    threshold; callers should usually route this through the Celery task
    so no request blocks on the provider call.

    Args:
        variant (ProductVariant): the variant that is low on stock.
        warehouse (Warehouse): the warehouse where stock is low.
        quantity (int): the available quantity observed.
        threshold (int): the configured low-stock threshold.

    Returns:
        list[NotificationLog]: the audit records for the staff sends.
    """
    from apps.accounts.models import User
    from apps.notifications.notification_templates import render_message

    message = render_message(
        "sms",
        "low_stock_alert",
        product_name=variant.name,
        sku=variant.sku,
        quantity=quantity,
        warehouse=warehouse.name,
    )

    staff_numbers = list(
        User.objects.filter(is_staff=True)
        .exclude(phone_number="")
        .values_list("phone_number", flat=True)
    )

    logs = []
    for phone in staff_numbers:
        log = send_sms(
            phone,
            message,
            purpose="transactional",
        )
        logs.append(log)

    return logs
