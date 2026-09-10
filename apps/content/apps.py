from django.apps import AppConfig


class ContentConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.content``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.content"
    verbose_name = "Content"

    def ready(self):
        """Import signal handlers so cache invalidation is wired at startup."""
        from apps.content import signals  # noqa: F401
