"""Data models for the inquiries app.

An ``Inquiry`` records the moment an anonymous storefront visitor taps
"Order via WhatsApp" or "Order via Email". The lines are snapshotted from
the visitor's server-side cart at capture time, so staff see exactly what
the visitor was looking at. Staff work this queue (contacted / converted /
abandoned) and link the converted ones to the ``Order`` created through
the staff intake view, which gives reporting a click-to-order conversion
funnel.
"""

import re
import secrets

from django.db import models

REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
SHORT_REFERENCE_PATTERN = re.compile(
    rf"^VF-?([{REFERENCE_ALPHABET}]{{4,8}})$"
)


def generate_reference_code():
    """Generate a unique short reference code such as ``VF-4821``.

    Uses an unambiguous character set (excluding ``0``, ``O``, ``1``, ``I``,
    ``L``) so staff and customers never misread characters over chat.

    Returns:
        str: a unique ``VF-XXXX`` reference code.
    """
    for length in (4, 4, 4, 5, 6):
        suffix = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(length))
        code = f"VF-{suffix}"
        if not Inquiry.objects.filter(reference_code=code).exists():
            return code
    suffix = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))
    return f"VF-{suffix}"


def normalize_short_reference(value):
    """Normalize a staff-entered short reference code to ``VF-XXXX`` form.

    Args:
        value: raw input string (for example ``vf 4k8p`` or ``VF-4K8P``).

    Returns:
        str | None: normalized ``VF-XXXX`` code, or ``None`` when invalid.
    """
    if value is None:
        return None
    compact = re.sub(r"[\s-]+", "", str(value).strip().upper())
    match = SHORT_REFERENCE_PATTERN.match(compact)
    if not match:
        return None
    return f"VF-{match.group(1)}"


class Inquiry(models.Model):
    """A captured WhatsApp/email hand-off from the anonymous storefront.

    ``cart_snapshot`` is staff reference data only  -  products, quantities,
    and displayed prices at click time, copied from the server-side cart.
    Prices are never re-charged from it; the staff intake view reprices
    everything server-side. ``contact_hint`` is the legacy combined summary,
    while the structured ``customer_*`` and delivery fields keep each fact
    addressable for staff search and the order-intake email. ``converted_order``
    is set when staff turn the conversation into a real order.

    ``reference_code`` (``VF-4K8P``) is a short, unique code embedded in the
    pre-filled WhatsApp/email message and page link so staff can match an
    incoming chat to its queue row.
    """

    CHANNEL_CHOICES = (
        ("whatsapp", "WhatsApp"),
        ("email", "Email"),
    )
    STATUS_CHOICES = (
        ("clicked", "Clicked"),
        ("message_received", "Message received"),
        ("new", "New"),
        ("contacted", "Contacted"),
        ("converted", "Converted"),
        ("abandoned", "Abandoned"),
        ("spam", "Spam"),
    )
    EMAIL_STATUS_CHOICES = (
        ("not_required", "Not required"),
        ("pending", "Pending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    )

    REFERENCE_PREFIX = "INQ"

    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    reference_code = models.CharField(
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Short unique code (e.g. VF-4K8P) embedded in hand-off messages.",
    )
    idempotency_key = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
        help_text="Client-supplied idempotency key for tap deduplication.",
    )
    recovery_token = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Opaque token authorizing post-tap recovery contact updates.",
    )
    page_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Storefront page URL where the visitor initiated the hand-off.",
    )
    cart_snapshot = models.JSONField(default=list)
    contact_hint = models.CharField(max_length=255, blank=True)
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_phone = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Normalized Kenyan mobile number in +254 form.",
    )
    customer_email = models.EmailField(blank=True, default="")
    delivery_location = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Area or landmark the visitor typed for delivery.",
    )
    order_notes = models.TextField(
        blank=True,
        default="",
        help_text="Optional instructions the visitor added with the inquiry.",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="clicked")
    message_received_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When staff confirmed the customer's chat message arrived.",
    )
    email_status = models.CharField(
        max_length=20,
        choices=EMAIL_STATUS_CHOICES,
        default="not_required",
        help_text="Delivery state of the order-intake email for email inquiries.",
    )
    email_error = models.TextField(
        blank=True,
        default="",
        help_text="Last delivery error, blank after a successful send.",
    )
    email_provider_message_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Provider message id returned by the last successful send.",
    )
    email_attempts = models.PositiveIntegerField(
        default=0,
        help_text="Number of order-intake email delivery attempts recorded.",
    )
    email_last_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the most recent delivery attempt was made.",
    )
    email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the order-intake email was last sent successfully.",
    )
    converted_order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inquiries",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "-created_at"], name="inq_status_created_idx"
            ),
            models.Index(
                fields=["channel", "-created_at"], name="inq_channel_created_idx"
            ),
            models.Index(
                fields=["email_status", "-created_at"],
                name="inq_email_status_created_idx",
            ),
            models.Index(fields=["created_at"], name="inq_created_idx"),
        ]

    def __str__(self):
        """Return a compact label with channel and status."""
        return f"Inquiry #{self.pk} ({self.channel}/{self.status})"

    def save(self, *args, **kwargs):
        """Ensure every inquiry has a unique short reference code and recovery token."""
        if not self.reference_code:
            self.reference_code = generate_reference_code()
        if not self.recovery_token:
            self.recovery_token = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)

    @property
    def reference(self):
        """Return the customer-facing reference code for this row.

        Returns:
            str: ``VF-XXXX`` (or legacy ``INQ-000123`` if unmigrated).
        """
        if self.reference_code:
            return self.reference_code
        if self.pk is None:
            return ""
        return f"{self.REFERENCE_PREFIX}-{self.pk:06d}"

    @staticmethod
    def parse_reference(value):
        """Extract an inquiry pk from a reference code or bare number.

        Accepts ``INQ-000123``, ``inq123``, ``INQ 123``, ``#123``, and plain
        ``123`` so staff can paste whatever a customer reads back over the
        phone without worrying about exact formatting.

        Args:
            value: the raw staff input.

        Returns:
            int | None: the candidate pk, or ``None`` when unparseable.
        """
        if value is None:
            return None
        text = str(value).strip().upper()
        for token in (Inquiry.REFERENCE_PREFIX, "#"):
            if text.startswith(token):
                text = text[len(token) :]
        text = text.strip().lstrip("-# ").strip()
        if not text.isdigit():
            return None
        return int(text)
