"""Signal handlers that keep the reviews feature flag coherent.

The ``enable_reviews`` toggle is cached so the per-request write gate is one
cache read. Any change to the configuration singleton must drop the cached
value or the flag could serve stale state until the TTL expires; this receiver
invalidates it on every ``SiteConfig`` save.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.models import SiteConfig
from apps.reviews import cache as reviews_cache


@receiver(post_save, sender=SiteConfig)
def on_site_config_saved(sender, instance, **kwargs):
    """Drop the cached feature flag when the configuration row changes."""
    reviews_cache.invalidate_feature_enabled()
