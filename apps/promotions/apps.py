from django.apps import AppConfig


class PromotionsConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.promotions``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.promotions"
    verbose_name = "Promotions"

    def ready(self):
        """Import signal handlers so price-cache invalidation is wired at startup."""
        from apps.promotions import signals  # noqa: F401
