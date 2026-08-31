"""Selectors for the core app.

Views stay thin; the singleton access pattern lives here so any future caller
(readers of the VAT rate, KRA PIN, or feature toggles) uses the same entry
point.
"""

from apps.core.models import SiteConfig


def get_site_config():
    """Return the singleton SiteConfig, creating the default row on first use.

    Returns:
        SiteConfig: the one-and-only configuration record.
    """
    return SiteConfig.load()
