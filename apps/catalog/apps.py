from django.apps import AppConfig


class CatalogConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.catalog``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog"
    verbose_name = "Catalog"
