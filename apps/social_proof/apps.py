from django.apps import AppConfig


class SocialProofConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.social_proof``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.social_proof"
    verbose_name = "Social Proof"

    def ready(self):
        """Import the social-proof signal receivers so they register on startup."""
        import apps.social_proof.signals  # noqa: F401
