"""Order-intake email delivery for storefront inquiries.

Email checkout inquiries are stored first and delivered to the shop mailbox
through Resend. Every attempt - successful or not - updates both the inquiry's
delivery fields and a ``NotificationLog`` row. Configuration problems are
recorded as failed attempts instead of being skipped silently.
"""

import logging
import traceback
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import mask_email
from apps.core.selectors import get_site_config
from apps.inquiries.models import Inquiry
from apps.inquiries.services import build_handoff_message
from apps.notifications.models import NotificationLog

logger = logging.getLogger(__name__)

_MISSING_RECIPIENT_ERROR = (
    "No order intake email is configured. Set SiteConfig.order_intake_email "
    "or ORDER_INTAKE_EMAIL."
)
_MISSING_API_KEY_ERROR = "RESEND_API_KEY is not configured; the email was not sent."
_MISSING_SENDER_ERROR = "ORDER_FROM_EMAIL is not configured; the email was not sent."
_SECRET_SETTING_NAMES = (
    "RESEND_API_KEY",
    "CELERY_BROKER_URL",
    "REDIS_URL",
    "DATABASE_URL",
)


def _redact_secrets(text):
    """Return text with configured infrastructure secrets removed.

    Args:
        text (str): the text to sanitise before logging or persisting.

    Returns:
        str: the text with every configured secret replaced.
    """
    for setting_name in _SECRET_SETTING_NAMES:
        secret = str(getattr(settings, setting_name, "") or "")
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _safe_error_text(exc):
    """Return exception text with configured secrets redacted.

    Args:
        exc (Exception): the provider or configuration exception.

    Returns:
        str: a displayable error string that never contains a secret.
    """
    text = str(exc) or exc.__class__.__name__
    return _redact_secrets(text)


def _log_exception_safely(logger, message, exc, *args):
    """Log an exception with a redacted traceback.

    Args:
        logger (logging.Logger): the logger to write to.
        message (str): a formatting string for the log message.
        exc (Exception): the exception to record.
        *args: values for the message formatting string.
    """
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    safe_message = message % args if args else message
    logger.error("%s\n%s", safe_message, _redact_secrets(trace))


def resolve_order_intake_recipient():
    """Return the mailbox that receives storefront order emails.

    Staff-editable ``SiteConfig.order_intake_email`` wins when set; the
    ``ORDER_INTAKE_EMAIL`` environment value is the deployment fallback.

    Returns:
        str: the configured recipient, or an empty string when neither is set.
    """
    configured = (get_site_config().order_intake_email or "").strip()
    if configured:
        return configured
    return (getattr(settings, "ORDER_INTAKE_EMAIL", "") or "").strip()


def _send_via_resend(*, recipient, subject, body, idempotency_key=None):
    """Send one email through Resend and normalise the provider result.

    The Resend SDK is imported lazily so a deployment that has not installed
    the optional package records a clear failed attempt instead of crashing at
    application import time.

    Args:
        recipient (str): the destination mailbox.
        subject (str): the email subject.
        body (str): the plain-text email body.
        idempotency_key (str | None): provider deduplication key for retries.

    Returns:
        dict: ``success``, ``message_id``, ``response``, ``error``, and
            ``retryable`` keys.
    """
    try:
        import resend
        from resend.exceptions import NoContentError, ResendError
    except ImportError as exc:
        return {
            "success": False,
            "message_id": "",
            "response": {},
            "error": _safe_error_text(exc),
            "retryable": False,
        }

    api_key = getattr(settings, "RESEND_API_KEY", "") or ""
    resend.api_key = api_key

    params = {
        "from": getattr(settings, "ORDER_FROM_EMAIL", ""),
        "to": [recipient],
        "subject": subject,
        "text": body,
    }
    options = {"idempotency_key": idempotency_key} if idempotency_key else None

    try:
        result = resend.Emails.send(params, options)
    except (NoContentError, ResendError, OSError, ValueError) as exc:
        _log_exception_safely(
            logger,
            "Resend order-intake email failed for inquiry recipient %s",
            exc,
            mask_email(recipient),
        )
        code = getattr(exc, "code", "")
        response = {
            "code": str(code),
            "type": getattr(exc, "error_type", type(exc).__name__),
            "message": _safe_error_text(exc),
            "retryable": str(code) == "429" or str(code).startswith("5"),
        }
        return {
            "success": False,
            "message_id": "",
            "response": response,
            "error": _safe_error_text(exc),
            "retryable": response["retryable"],
        }

    message_id = ""
    if isinstance(result, dict):
        message_id = str(result.get("id", ""))
    else:
        message_id = str(getattr(result, "id", ""))
    return {
        "success": True,
        "message_id": message_id,
        "response": {"id": message_id},
        "error": "",
        "retryable": False,
    }


def _order_intake_subject(inquiry):
    """Return the mailbox subject for one inquiry.

    Args:
        inquiry (Inquiry): the captured inquiry.

    Returns:
        str: a subject carrying the reference and line count.
    """
    line_count = len(inquiry.cart_snapshot or [])
    return (
        f"New storefront order inquiry {inquiry.reference} "
        f"({line_count} line{'s' if line_count != 1 else ''})"
    )


def _order_intake_body(inquiry):
    """Return the plain-text mailbox body for one inquiry.

    Args:
        inquiry (Inquiry): the captured inquiry.

    Returns:
        str: contact details followed by the canonical hand-off message.
    """
    lines = [
        "A customer submitted an order inquiry from the storefront.",
        f"Reference: {inquiry.reference}",
        f"Channel: {inquiry.get_channel_display()}",
    ]
    if inquiry.contact_hint:
        lines.append(f"Customer contact: {inquiry.contact_hint}")
    lines.extend(
        [
            "",
            build_handoff_message(
                cart_snapshot=inquiry.cart_snapshot,
                reference=inquiry.reference,
            ),
        ]
    )
    return "\n".join(lines)


def _mark_attempt_started(inquiry):
    """Record that a delivery attempt has started on an inquiry.

    Args:
        inquiry (Inquiry): the inquiry being delivered.
    """
    inquiry.email_status = "pending"
    inquiry.email_attempts = (inquiry.email_attempts or 0) + 1
    inquiry.email_last_attempt_at = timezone.now()
    inquiry.save(
        update_fields=[
            "email_status",
            "email_attempts",
            "email_last_attempt_at",
            "updated_at",
        ]
    )


def _update_inquiry_delivery(inquiry, *, status, error="", provider_message_id=""):
    """Persist the latest delivery outcome on an inquiry.

    Args:
        inquiry (Inquiry): the inquiry to update.
        status (str): one of ``Inquiry.EMAIL_STATUS_CHOICES``.
        error (str): failure text, blank on success.
        provider_message_id (str): Resend message id on success.
    """
    inquiry.email_status = status
    inquiry.email_error = error
    if status == "sent":
        inquiry.email_provider_message_id = provider_message_id
        inquiry.email_sent_at = timezone.now()
    inquiry.save(
        update_fields=[
            "email_status",
            "email_error",
            "email_provider_message_id",
            "email_sent_at",
            "updated_at",
        ]
    )


def _record_failed_log(*, recipient, subject, body, purpose, error, sent_by=None):
    """Create a failed ``NotificationLog`` row for a send that never happened.

    Args:
        recipient (str): the intended recipient, possibly empty.
        subject (str): the intended subject.
        body (str): the intended body.
        purpose (str): the notification purpose.
        error (str): why the send was skipped.
        sent_by: the staff user who triggered a manual resend, if any.

    Returns:
        NotificationLog: the failed audit row.
    """
    return NotificationLog.objects.create(
        channel="email",
        purpose=purpose,
        recipient=mask_email(recipient),
        message=f"[Subject] {subject}\n\n{body[:500]}",
        status="failed",
        error_message=error,
        sent_by=sent_by,
    )


def _deliver_order_intake_email(
    *,
    recipient,
    subject,
    body,
    purpose,
    inquiry=None,
    sent_by=None,
    idempotency_key=None,
):
    """Deliver one order-intake email and record the attempt.

    Args:
        recipient (str): the destination mailbox.
        subject (str): the email subject.
        body (str): the plain-text body.
        purpose (str): the ``NotificationLog`` purpose.
        inquiry (Inquiry | None): the inquiry being delivered, if any.
        sent_by: the staff user who triggered a manual resend, if any.
        idempotency_key (str | None): provider deduplication key.

    Returns:
        NotificationLog: the audit row for this attempt.
    """
    log = NotificationLog.objects.create(
        channel="email",
        purpose=purpose,
        recipient=mask_email(recipient),
        message=f"[Subject] {subject}\n\n{body[:500]}",
        sent_by=sent_by,
    )

    if inquiry is not None:
        _mark_attempt_started(inquiry)

    if not recipient:
        logger.error(
            "Order-intake email skipped for %s: %s",
            inquiry.reference if inquiry is not None else "test send",
            _MISSING_RECIPIENT_ERROR,
        )
        log.update_status("failed", error_message=_MISSING_RECIPIENT_ERROR)
        if inquiry is not None:
            _update_inquiry_delivery(
                inquiry,
                status="failed",
                error=_MISSING_RECIPIENT_ERROR,
            )
        return log

    if not (getattr(settings, "RESEND_API_KEY", "") or "").strip():
        logger.error(
            "Order-intake email skipped for %s: %s",
            inquiry.reference if inquiry is not None else "test send",
            _MISSING_API_KEY_ERROR,
        )
        log.update_status("failed", error_message=_MISSING_API_KEY_ERROR)
        if inquiry is not None:
            _update_inquiry_delivery(
                inquiry,
                status="failed",
                error=_MISSING_API_KEY_ERROR,
            )
        return log

    sender = (getattr(settings, "ORDER_FROM_EMAIL", "") or "").strip()
    if not sender:
        logger.error(
            "Order-intake email skipped for %s: %s",
            inquiry.reference if inquiry is not None else "test send",
            _MISSING_SENDER_ERROR,
        )
        log.update_status("failed", error_message=_MISSING_SENDER_ERROR)
        if inquiry is not None:
            _update_inquiry_delivery(
                inquiry,
                status="failed",
                error=_MISSING_SENDER_ERROR,
            )
        return log

    result = _send_via_resend(
        recipient=recipient,
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
    )
    if result["success"]:
        log.update_status(
            "sent",
            provider_message_id=result["message_id"],
            provider_response=result["response"],
        )
        if inquiry is not None:
            _update_inquiry_delivery(
                inquiry,
                status="sent",
                provider_message_id=result["message_id"],
            )
    else:
        error = result["error"] or "Unknown email provider failure."
        log.update_status(
            "failed",
            provider_response=result["response"],
            error_message=error,
        )
        if inquiry is not None:
            _update_inquiry_delivery(
                inquiry,
                status="failed",
                error=error,
            )
    return log


def send_order_intake_email(inquiry, *, sent_by=None, idempotency_key=None):
    """Send one inquiry's order-intake email through Resend.

    Args:
        inquiry (Inquiry): the captured inquiry to deliver.
        sent_by: the staff user who triggered a manual resend, if any.
        idempotency_key (str | None): provider deduplication key; defaults to
            the stable key used by automatic retries.

    Returns:
        NotificationLog: the audit row for this attempt.

    Raises:
        ValueError: when the inquiry did not come through the email channel.
    """
    if inquiry.channel != "email":
        raise ValueError("Only email inquiries have an order-intake email.")
    recipient = resolve_order_intake_recipient()
    return _deliver_order_intake_email(
        recipient=recipient,
        subject=_order_intake_subject(inquiry),
        body=_order_intake_body(inquiry),
        purpose="order_intake",
        inquiry=inquiry,
        sent_by=sent_by,
        idempotency_key=idempotency_key or f"order-intake-inquiry-{inquiry.pk}",
    )


def send_order_intake_test_email():
    """Send a real test email to ``ORDER_INTAKE_EMAIL`` through Resend.

    Returns:
        NotificationLog: the audit row for the test attempt.
    """
    recipient = (getattr(settings, "ORDER_INTAKE_EMAIL", "") or "").strip()
    subject = "Test order intake email"
    body = (
        "This is a test of the Vifaar order-intake email path. "
        "No customer order is attached."
    )
    return _deliver_order_intake_email(
        recipient=recipient,
        subject=subject,
        body=body,
        purpose="test",
    )


def _record_enqueue_failure(inquiry_id, exc):
    """Record a failure to enqueue an inquiry email task.

    Args:
        inquiry_id (int): the inquiry whose task could not be queued.
        exc (Exception): the broker or task-enqueue error.
    """
    error = f"Could not enqueue the order-intake email: {_safe_error_text(exc)}"
    inquiry = Inquiry.objects.filter(pk=inquiry_id).first()
    if inquiry is None:
        return
    inquiry.email_status = "failed"
    inquiry.email_error = error
    inquiry.save(update_fields=["email_status", "email_error", "updated_at"])
    _record_failed_log(
        recipient=resolve_order_intake_recipient(),
        subject=_order_intake_subject(inquiry),
        body=_order_intake_body(inquiry),
        purpose="order_intake",
        error=error,
    )


def queue_order_intake_email(inquiry):
    """Queue delivery of an inquiry's order-intake email after commit.

    Only email-channel inquiries are queued; WhatsApp inquiries continue in
    the chat and do not need a mailbox copy. A broker failure is logged and
    recorded on the inquiry instead of being swallowed.

    Args:
        inquiry (Inquiry): the freshly stored inquiry.
    """
    if inquiry.channel != "email":
        return

    inquiry_id = inquiry.pk

    def enqueue():
        """Hand the inquiry id to Celery after the database commit."""
        from apps.inquiries.tasks import send_order_intake_email_task

        try:
            send_order_intake_email_task.delay(inquiry_id)
        except Exception as exc:
            _log_exception_safely(
                logger,
                "Could not enqueue order-intake email for inquiry %s",
                exc,
                inquiry_id,
            )
            _record_enqueue_failure(inquiry_id, exc)

    transaction.on_commit(enqueue)


def resend_order_intake_email(inquiry, *, sent_by=None):
    """Send an inquiry's email again from the staff console.

    Each staff request gets a fresh provider idempotency key so a deliberate
    resend is not suppressed by the key used for automatic retries.

    Args:
        inquiry (Inquiry): the inquiry to resend.
        sent_by: the staff user performing the resend.

    Returns:
        NotificationLog: the audit row for this attempt.
    """
    idempotency_key = f"order-intake-inquiry-{inquiry.pk}-resend-{uuid.uuid4().hex}"
    return send_order_intake_email(
        inquiry,
        sent_by=sent_by,
        idempotency_key=idempotency_key,
    )
