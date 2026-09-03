from django.apps import AppConfig


class BundlesConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.bundles``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.bundles"
    verbose_name = "Bundles"

    def ready(self):
        """Import signal handlers so cache invalidation is wired at startup."""
        from apps.bundles import signals  # noqa: F401
