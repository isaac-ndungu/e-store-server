from django.apps import AppConfig


class ShippingConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.shipping``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.shipping"
    verbose_name = "Shipping"
