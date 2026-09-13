"""Notification message templates.

Central registry of outbound message bodies keyed by ``(channel, purpose)``.
Messages are rendered with ``str.format()`` from a per-purpose context, so
message copy lives in one place and the same wording is reused by both the
synchronous service functions and the asynchronous Celery tasks. Keeping the
copy here (rather than hardcoded in each caller) makes it straightforward to
localise later.

Templates for purposes whose producing apps do not exist yet (order events,
abandoned carts) are present so the sending machinery is complete before the
downstream app that triggers them arrives.
"""

_template_sms = {
    "order_confirmation": (
        "Order {order_id} confirmed! Total: KES {total}. "
        "Thank you for shopping with us."
    ),
    "order_status_update": ("Your order {order_id} status is now {status}. {detail}"),
    "delivery_update": ("Your order {order_id} is {delivery_status}. {eta_info}"),
    "transactional": "{body}",
    "test": "{body}",
}

_template_email = {
    "order_confirmation": (
        "Your order {order_id} has been confirmed. Total: KES {total}. "
        "We will notify you when it ships."
    ),
    "transactional": "{body}",
    "test": "{body}",
}

_TEMPLATES = {
    "sms": _template_sms,
    "email": _template_email,
}


def render_message(channel, purpose, **context):
    """Render a message body for a channel and purpose.

    Args:
        channel (str): ``"sms"`` or ``"email"``.
        purpose (str): template key for the channel.
        **context: values substituted into the template.

    Returns:
        str: the rendered message body.

    Raises:
        ValueError: if the channel or purpose has no registered template.
    """
    templates = _TEMPLATES.get(channel)
    if templates is None:
        raise ValueError(f"Unsupported template channel: {channel}")
    template = templates.get(purpose)
    if template is None:
        raise ValueError(f"No template registered for ({channel}, {purpose})")
    return template.format(**context)
