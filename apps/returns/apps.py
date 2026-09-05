from django.apps import AppConfig


class ReturnsConfig(AppConfig):
    """Django app configuration for the returns module."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.returns"
    verbose_name = "Returns"
