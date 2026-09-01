from django.db import models


class NotificationLog(models.Model):
    """Immutable record of a single outbound notification (SMS, email, etc.).

    Created by the service layer after every successful or failed send so
    the audit trail is complete regardless of provider outcome. The log is
    append-only — status transitions are recorded as new rows via
    ``update_status()`` rather than in-place edits, preserving the full
    history of each send attempt.
    """

    CHANNEL_CHOICES = (
        ("sms", "SMS"),
        ("email", "Email"),
    )
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("failed", "Failed"),
    )
    PURPOSE_CHOICES = (
        ("test", "Internal Test"),
        ("otp", "One-Time Password"),
        ("order_update", "Order Status Update"),
        ("promotional", "Promotional"),
        ("transactional", "Transactional"),
    )

    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default="sms")
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default="test")
    recipient = models.CharField(
        max_length=15,
        help_text="Phone number (E.164) or email address.",
    )
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    sent_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_notifications",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "channel"]),
            models.Index(fields=["status"]),
            models.Index(fields=["purpose"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        """Return a human-readable label for admin/trace output."""
        return f"[{self.get_channel_display()}] {self.recipient} ({self.status})"

    def update_status(
        self, status, provider_message_id="", provider_response=None, error_message=""
    ):
        """Transition the log to a new status and persist immediately.

        Args:
            status (str): the new status from ``STATUS_CHOICES``.
            provider_message_id (str): provider-assigned message ID, if any.
            provider_response (dict | None): raw provider response payload.
            error_message (str): error text when the status is ``failed``.
        """
        self.status = status
        if provider_message_id:
            self.provider_message_id = provider_message_id
        if provider_response is not None:
            self.provider_response = provider_response
        if error_message:
            self.error_message = error_message
        self.save(
            update_fields=[
                "status",
                "provider_message_id",
                "provider_response",
                "error_message",
                "updated_at",
            ]
        )
