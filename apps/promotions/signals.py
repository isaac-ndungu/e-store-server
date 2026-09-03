"""Signal handlers that keep promotion price caches coherent.

Effective prices depend on the base variant price and the set of active
discounts. Because a single discount change can affect many variants at once
(a sitewide or category sale), the effective-price cache uses a generation
counter: every discount or variant-price change bumps it, which invalidates
all cached effective prices at once. The same change can alter the
discount-aware component prices of bundles, so bundle price caches are
cleared too.
"""

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.catalog.models import ProductVariant
from apps.promotions import cache as promotions_cache
from apps.promotions.models import Discount


def _invalidate_on_discount_change():
    """Bump the effective-price generation and clear bundle price caches.

    A discount change can affect any variant and any bundle, so both caches
    are invalidated wholesale rather than per-key.
    """
    promotions_cache.bump_discount_generation()
    from apps.bundles.cache import invalidate_all_bundle_prices

    invalidate_all_bundle_prices()


@receiver(post_save, sender=Discount)
def on_discount_saved(sender, instance, **kwargs):
    """Invalidate effective prices and bundle prices when a discount changes."""
    _invalidate_on_discount_change()


@receiver(post_delete, sender=Discount)
def on_discount_deleted(sender, instance, **kwargs):
    """Invalidate effective prices and bundle prices when a discount is removed."""
    _invalidate_on_discount_change()


def _variant_price_changed(instance):
    """Return whether a variant save changes its price.

    Fired from ``pre_save``, where the database still holds the prior row: a
    new variant or a row whose stored price differs from the instance's price
    is a change that must refresh cached prices.

    Args:
        instance (ProductVariant): the variant being saved.

    Returns:
        bool: True when the price changed or the variant is new.
    """
    if instance.pk is None:
        return True
    previous = (
        ProductVariant.objects.filter(pk=instance.pk)
        .values_list("price", flat=True)
        .first()
    )
    return previous != instance.price


@receiver(pre_save, sender=ProductVariant)
def on_variant_saved(sender, instance, **kwargs):
    """Invalidate effective prices when a variant's price is about to change.

    Bundle price invalidation on a variant change is handled by the bundles
    app's own signals, which already track which bundles reference the
    variant; here only the generation is bumped so effective prices refresh.
    """
    if _variant_price_changed(instance):
        promotions_cache.bump_discount_generation()


@receiver(post_delete, sender=ProductVariant)
def on_variant_deleted(sender, instance, **kwargs):
    """Invalidate effective prices when a variant is removed."""
    promotions_cache.bump_discount_generation()
