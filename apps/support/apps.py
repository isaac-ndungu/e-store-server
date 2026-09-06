from django.apps import AppConfig


class SupportConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.support``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.support"
    verbose_name = "Support"
