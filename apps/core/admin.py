"""Admin editor for the core app's singleton configuration record.

The model's ``save()`` already pins the row to ``pk=1``; these overrides make
the singleton intent explicit in the UI by forbidding adds and deletes so
staff can only edit the one existing record.
"""

from django.contrib import admin

from apps.core.models import SiteConfig


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    """Admin page exposing the single runtime configuration row for editing."""

    list_display = ("site_name", "standard_vat_rate", "kra_pin", "support_phone")

    def has_add_permission(self, request):
        """Return False: there must never be a second config row."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Return False: the config row must never be deleted."""
        return False
