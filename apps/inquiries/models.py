"""Data models for the inquiries app.

An ``Inquiry`` records the moment an anonymous storefront visitor taps
"Order via WhatsApp" or "Order via Email". The lines are snapshotted from
the visitor's server-side cart at capture time, so staff see exactly what
the visitor was looking at. Staff work this queue (contacted / converted /
abandoned) and link the converted ones to the ``Order`` created through
the staff intake view, which gives reporting a click-to-order conversion
funnel.
"""

from django.db import models


class Inquiry(models.Model):
    """A captured WhatsApp/email hand-off from the anonymous storefront.

    ``cart_snapshot`` is staff reference data only — products, quantities,
    and displayed prices at click time, copied from the server-side cart.
    Prices are never re-charged from it; the staff intake view reprices
    everything server-side. ``contact_hint``
    is whatever the storefront knew (a typed phone/email), blank when the
    visitor gave nothing. ``converted_order`` is set when staff turn the
    conversation into a real order.

    ``reference`` (``INQ-000123``) is the code the storefront embeds in the
    pre-filled WhatsApp/email message so staff can match an incoming chat to
    its queue row instead of eyeballing cart contents. It derives from the
    pk, so no extra column or uniqueness machinery is needed.
    """

    CHANNEL_CHOICES = (
        ("whatsapp", "WhatsApp"),
        ("email", "Email"),
    )
    STATUS_CHOICES = (
        ("new", "New"),
        ("contacted", "Contacted"),
        ("converted", "Converted"),
        ("abandoned", "Abandoned"),
    )

    REFERENCE_PREFIX = "INQ"

    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    cart_snapshot = models.JSONField(default=list)
    contact_hint = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new")
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
        ]

    def __str__(self):
        """Return a compact label with channel and status."""
        return f"Inquiry #{self.pk} ({self.channel}/{self.status})"

    @property
    def reference(self):
        """Return the customer-facing reference code for this row.

        Returns:
            str: ``INQ-000123`` once saved, else an empty string.
        """
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
