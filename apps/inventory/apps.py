from django.apps import AppConfig


class InventoryConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.inventory``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    verbose_name = "Inventory"
