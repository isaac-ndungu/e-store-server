"""App configuration for the accounts app.

Hosts the custom ``User`` model and ``Address``. Because ``User`` replaces
Django's default auth model, this app must be present in ``INSTALLED_APPS`` and
``AUTH_USER_MODEL`` must point at it before the first migration of the project
runs — see settings.
"""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.accounts``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Accounts"
