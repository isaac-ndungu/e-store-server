"""App configuration for the notifications app.

Provides the SMS-sending abstraction (Africa's Talking) used by every
downstream app that needs to deliver OTPs, order confirmations, or
promotional messages.
"""

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.notifications``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    verbose_name = "Notifications"
