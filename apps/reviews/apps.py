from django.apps import AppConfig


class ReviewsConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.reviews``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reviews"
    verbose_name = "Reviews"

    def ready(self):
        """Import the reviews signal receivers so they register on startup."""
        import apps.reviews.signals  # noqa: F401
