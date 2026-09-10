"""Signal handlers that keep the storefront content cache coherent.

Banner rotations and published content pages are cached for storefront
reads. Any change to a banner or page — including a schedule or
publication-state edit that changes what visitors see — invalidates the
matching cache generation so a stale payload is never served.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.content import cache as content_cache
from apps.content.models import Banner, ContentPage


@receiver(post_save, sender=Banner)
def on_banner_saved(sender, instance, **kwargs):
    """Invalidate cached banner rows when a banner changes."""
    content_cache.bump_banner_generation()


@receiver(post_delete, sender=Banner)
def on_banner_deleted(sender, instance, **kwargs):
    """Invalidate cached banner rows when a banner is deleted."""
    content_cache.bump_banner_generation()


@receiver(post_save, sender=ContentPage)
def on_page_saved(sender, instance, **kwargs):
    """Invalidate cached pages when a content page changes."""
    content_cache.bump_page_generation()


@receiver(post_delete, sender=ContentPage)
def on_page_deleted(sender, instance, **kwargs):
    """Invalidate cached pages when a content page is deleted."""
    content_cache.bump_page_generation()
