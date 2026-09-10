"""Data models for the inquiries app.

An ``Inquiry`` records the moment an anonymous storefront visitor taps
"Order via WhatsApp" or "Order via Email". The visitor's cart never touches
the backend — only a snapshot of what they were looking at, plus the channel
they chose. Staff work this queue (contacted / converted / abandoned) and
link the converted ones to the ``Order`` created through the staff intake
view, which gives reporting a click-to-order conversion funnel.
"""

from django.db import models


class Inquiry(models.Model):
    """A captured WhatsApp/email hand-off from the anonymous storefront.

    ``cart_snapshot`` is staff reference data only — products, quantities,
    and displayed prices at click time. Prices are never re-charged from it;
    the staff intake view reprices everything server-side. ``contact_hint``
    is whatever the storefront knew (a typed phone/email), blank when the
    visitor gave nothing. ``converted_order`` is set when staff turn the
    conversation into a real order.
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
