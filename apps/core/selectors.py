"""Selectors for the core app.

Views stay thin; the singleton access pattern lives here so any future caller
(readers of the VAT rate, KRA PIN, or feature toggles) uses the same entry
point.
"""

from apps.core import cache as core_cache
from apps.core.models import SiteConfig


def get_site_config():
    """Return the singleton SiteConfig, creating the default row on first use.

    Field values are cached briefly because the storefront reads this row on
    cold load and feature toggles are checked per request; the cache is
    dropped whenever the singleton is saved so staff edits apply promptly.

    Returns:
        SiteConfig: the one-and-only configuration record.
    """
    cached = core_cache.get_cached_site_config()
    if cached is not None:
        obj = SiteConfig(pk=1)
        for field, value in cached.items():
            setattr(obj, field, value)
        obj._state.adding = False
        return obj
    obj = SiteConfig.load()
    core_cache.cache_site_config(
        {
            "site_name": obj.site_name,
            "tagline": obj.tagline,
            "domain": obj.domain,
            "logo": obj.logo.name if obj.logo else "",
            "favicon": obj.favicon.name if obj.favicon else "",
            "theme": obj.theme,
            "settings": obj.settings,
            "contact_email": obj.contact_email,
            "support_phone": obj.support_phone,
            "whatsapp_number": obj.whatsapp_number,
            "order_intake_email": obj.order_intake_email,
            "facebook_url": obj.facebook_url,
            "instagram_url": obj.instagram_url,
            "tiktok_url": obj.tiktok_url,
            "social_links": obj.social_links,
            "kra_pin": obj.kra_pin,
            "business_registration_number": obj.business_registration_number,
            "physical_address": obj.physical_address,
            "return_policy_text": obj.return_policy_text,
            "cooling_off_period_days": obj.cooling_off_period_days,
            "standard_vat_rate": obj.standard_vat_rate,
        }
    )
    return obj
