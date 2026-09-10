"""App configuration for the audit app.

Hosts the consolidated security tests — cross-user access (IDOR) coverage,
the endpoint security-posture audit, and the concurrency tests for the
stock-reservation and payment-callback paths. The app ships no models or
endpoints; it exists so the checks run under the regular test runner.
"""

from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Django app config: lives under ``apps/`` and is referenced as ``apps.audit``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
    verbose_name = "Audit"
