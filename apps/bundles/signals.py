"""Signal handlers that keep the bundle price cache coherent.

A bundle's price is derived from its own discount fields and its components'
product prices. Any change to the bundle or to one of its items invalidates
the cached price so the storefront never serves a stale figure.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.bundles import cache as bundle_cache
from apps.bundles.models import Bundle, BundleItem
from apps.catalog.models import Product


def _invalidate(bundle):
    """Drop the cached price for a bundle, if any.

    Args:
        bundle (Bundle | None): the affected bundle.
    """
    if bundle is not None:
        bundle_cache.invalidate_bundle_price(bundle.slug)


@receiver(post_save, sender=Bundle)
def on_bundle_saved(sender, instance, **kwargs):
    """Invalidate the cache when a bundle's fields change."""
    _invalidate(instance)


@receiver(post_delete, sender=Bundle)
def on_bundle_deleted(sender, instance, **kwargs):
    """Invalidate the cache when a bundle is deleted."""
    _invalidate(instance)


@receiver(post_save, sender=BundleItem)
def on_bundle_item_saved(sender, instance, **kwargs):
    """Invalidate the cache when a component changes."""
    _invalidate(instance.bundle)


@receiver(post_delete, sender=BundleItem)
def on_bundle_item_deleted(sender, instance, **kwargs):
    """Invalidate the cache when a component is removed."""
    _invalidate(instance.bundle)


def _invalidate_bundles_for_product(product):
    """Drop cached prices for bundles containing a product.

    A bundle's regular price depends on its components' product prices. When
    a product's price changes or it is deleted, every bundle containing that
    product is invalidated.

    Args:
        product (Product): the changed catalogue product.
    """
    bundle_ids = (
        BundleItem.objects.filter(product_id=product.pk)
        .values_list("bundle_id", flat=True)
        .distinct()
    )
    for bundle in Bundle.objects.filter(pk__in=bundle_ids).only("slug"):
        bundle_cache.invalidate_bundle_price(bundle.slug)


@receiver(post_save, sender=Product)
def on_product_saved(sender, instance, **kwargs):
    """Invalidate affected bundle caches when a product's price changes."""
    _invalidate_bundles_for_product(instance)


@receiver(post_delete, sender=Product)
def on_product_deleted(sender, instance, **kwargs):
    """Invalidate affected bundle caches when a product is removed."""
    _invalidate_bundles_for_product(instance)
