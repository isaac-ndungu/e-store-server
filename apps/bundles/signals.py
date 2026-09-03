"""Signal handlers that keep the bundle price cache coherent.

A bundle's price is derived from its own discount fields and its components'
variant prices. Any change to the bundle or to one of its items invalidates
the cached price so the storefront never serves a stale figure.
"""

from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.bundles import cache as bundle_cache
from apps.bundles.models import Bundle, BundleItem
from apps.catalog.models import ProductVariant


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


def _invalidate_bundles_for_variant(variant):
    """Drop cached prices for bundles whose price derives from a variant.

    A bundle's regular price depends on the variants it references explicitly
    and on the cheapest active variant of a product-only item. When a variant's
    price changes or it is deleted, either source can change, so every bundle
    touching the variant or its product is invalidated.

    Args:
        variant (ProductVariant): the changed catalogue variant.
    """
    item_ids = BundleItem.objects.filter(
        Q(variant=variant) | Q(product=variant.product_id)
    ).values_list("pk", flat=True)
    bundle_ids = BundleItem.objects.filter(pk__in=item_ids).values_list(
        "bundle_id", flat=True
    )
    for bundle in Bundle.objects.filter(pk__in=bundle_ids).only("slug"):
        bundle_cache.invalidate_bundle_price(bundle.slug)


@receiver(post_save, sender=ProductVariant)
def on_variant_saved(sender, instance, **kwargs):
    """Invalidate affected bundle caches when a variant's price changes."""
    _invalidate_bundles_for_variant(instance)


@receiver(post_delete, sender=ProductVariant)
def on_variant_deleted(sender, instance, **kwargs):
    """Invalidate affected bundle caches when a variant is removed."""
    _invalidate_bundles_for_variant(instance)
