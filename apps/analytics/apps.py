from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.analytics``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    verbose_name = "Analytics"
