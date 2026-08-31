"""App configuration for the core app.

Everything downstream — shipping VAT, residency of tax data, feature toggles,
storefront branding — reads from the core app's ``SiteConfig`` singleton.
"""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.core``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"
