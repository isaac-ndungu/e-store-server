"""Celery tasks for inquiry email delivery.

The public inquiry endpoint must return quickly, so delivery to Resend runs in
the worker. Each retry records its own attempt, and the Resend idempotency key
keeps duplicate deliveries from a retry from sending twice.
"""

import logging

from celery import shared_task

from apps.inquiries.models import Inquiry

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def send_order_intake_email_task(self, inquiry_id):
    """Send one inquiry's order-intake email, retrying transient failures.

    The task is idempotent across retries: the delivery service uses a stable
    Resend idempotency key per inquiry, and every attempt is persisted.

    Args:
        inquiry_id (int): the inquiry primary key.

    Returns:
        dict: outcome summary with the inquiry id, log id, and status.
    """
    from apps.inquiries.email import send_order_intake_email

    try:
        inquiry = Inquiry.objects.get(pk=inquiry_id)
    except Inquiry.DoesNotExist:
        logger.warning(
            "Order-intake email skipped: inquiry %s no longer exists", inquiry_id
        )
        return {"inquiry_id": inquiry_id, "status": "missing"}

    if inquiry.email_status == "sent":
        logger.info(
            "Order-intake email already sent for inquiry %s; skipping redelivery",
            inquiry_id,
        )
        return {"inquiry_id": inquiry_id, "status": "already_sent"}

    log = send_order_intake_email(inquiry)
    if log.status != "sent" and log.provider_response.get("retryable"):
        raise self.retry(countdown=60 * (2**self.request.retries))

    return {
        "inquiry_id": inquiry_id,
        "notification_log_id": log.pk,
        "status": log.status,
    }
