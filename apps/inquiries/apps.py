from django.apps import AppConfig


class InquiriesConfig(AppConfig):
    """Django app config for the assisted-sales hand-off queue."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inquiries"
    verbose_name = "Inquiries"
