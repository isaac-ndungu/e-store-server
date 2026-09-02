from django.apps import AppConfig


class CollectionsConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.collections``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.collections"
    verbose_name = "Collections"

    def ready(self):
        """Import signal handlers so cache invalidation is wired at startup."""
        from apps.collections import signals  # noqa: F401
