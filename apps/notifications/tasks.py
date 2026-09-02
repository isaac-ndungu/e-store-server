"""Celery tasks for the notifications app.

Outbound notification sends are async so the request/response cycle never
blocks on a provider network call, and failed sends can be retried.  Every
task here is idempotent and safe to run twice: the underlying service
functions dedupe via ``idempotency_key`` and tasks check state before
acting, because Celery may redeliver a task after a worker failure.

Tasks whose producing app does not exist yet (order events, abandoned
carts) are declared as stubs so the sending infrastructure is complete
before the downstream trigger arrives.
"""

from celery import shared_task

from apps.notifications.services import send_email, send_sms


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(IOError, OSError),
    retry_backoff=True,
)
def send_sms_task(
    self,
    recipient,
    message,
    purpose="transactional",
    sent_by_id=None,
    sender_id=None,
    log_message=None,
    idempotency_key=None,
):
    """Send an SMS in the background, retrying on transient failures.

    Idempotent: passing the same ``idempotency_key`` twice only sends (and
    logs) once, so a redelivery cannot double-charge for an SMS.

    Args:
        self: the bound task instance (Celery).
        recipient (str): E.164 phone number.
        message (str): SMS body text.
        purpose (str): notification purpose.
        sent_by_id (int | None): id of the staff user who triggered the send.
        sender_id (str | None): override sender ID.
        log_message (str | None): text to persist in the audit log.
        idempotency_key (str | None): dedup key for the send.

    Returns:
        int: the id of the audit log for this send.
    """
    sent_by = _resolve_sent_by(sent_by_id)
    try:
        log = send_sms(
            recipient,
            message,
            purpose=purpose,
            sent_by=sent_by,
            sender_id=sender_id,
            log_message=log_message,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        self.retry(exc=exc)
    return log.id


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(IOError, OSError),
    retry_backoff=True,
)
def send_email_task(
    self,
    recipient,
    subject,
    body,
    purpose="transactional",
    sent_by_id=None,
    html_body=None,
):
    """Send an email in the background, retrying on transient failures.

    Idempotent: a redelivery after failure is safe because the service
    function records the outcome and a duplicate call only produces
    another audit row, never a double charge.

    Args:
        self: the bound task instance (Celery).
        recipient (str): email address.
        subject (str): email subject.
        body (str): plaintext email body.
        purpose (str): notification purpose.
        sent_by_id (int | None): id of the staff user who triggered the send.
        html_body (str | None): optional HTML body.

    Returns:
        int: the id of the audit log for this send.
    """
    sent_by = _resolve_sent_by(sent_by_id)
    try:
        log = send_email(
            recipient,
            subject,
            body,
            purpose=purpose,
            sent_by=sent_by,
            html_body=html_body,
        )
    except Exception as exc:
        self.retry(exc=exc)
    return log.id


def _resolve_sent_by(sent_by_id):
    """Resolve a staff user id to a User instance, or None.

    Args:
        sent_by_id (int | None): the user's primary key.

    Returns:
        User | None: the matching user, or None if the id is missing or the
            user no longer exists.
    """
    if sent_by_id is None:
        return None
    from apps.accounts.models import User

    return User.objects.filter(pk=sent_by_id).first()


# --- Stubs for downstream apps (order events, abandoned cart) ---


@shared_task
def send_order_notification_task(order_id, purpose, **context):
    """Send an order-related notification (stub).

    Implemented once the orders app exists.  Will look up the order by
    ``order_id``, read its contact phone/email, render the matching
    template from the notification registry, and dispatch it in the
    background.

    Args:
        order_id (int): the order this notification relates to.
        purpose (str): notification purpose (e.g. ``order_confirmation``).
        **context: template rendering context.

    Returns:
        None: placeholder until wired to the orders app.
    """
    return None


@shared_task
def send_abandoned_cart_reminder_task(cart_id):
    """Send an abandoned-cart reminder (stub).

    Implemented once the cart app exists.  Will compose a reminder from
    the ``abandoned_cart`` template and send it to the cart's contact.

    Args:
        cart_id (int): the cart to remind about.

    Returns:
        None: placeholder until wired to the cart app.
    """
    return None
