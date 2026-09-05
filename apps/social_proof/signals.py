"""Signal handlers that keep the social-proof feature flag coherent.

The ``enable_social_proof`` toggle is cached so the per-request gate is one
cache read. Any change to the configuration singleton must drop the cached
value or the flag could serve stale state until the TTL expires; this receiver
invalidates it on every ``SiteConfig`` save.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.models import SiteConfig
from apps.social_proof import cache as proof_cache


@receiver(post_save, sender=SiteConfig)
def on_site_config_saved(sender, instance, **kwargs):
    """Drop the cached feature flag when the configuration row changes."""
    proof_cache.invalidate_feature_enabled()
