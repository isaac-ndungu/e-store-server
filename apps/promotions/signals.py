"""Signal handlers that keep promotion price caches coherent.

Effective prices depend on the base product price and the set of active
discounts. Because a single discount change can affect many products at once
(a sitewide or category sale), the effective-price cache uses a generation
counter: every discount or product-price change bumps it, which invalidates
all cached effective prices at once. The same change can alter the
discount-aware component prices of bundles, so bundle price caches are
cleared too.
"""

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.catalog.models import Product
from apps.promotions import cache as promotions_cache
from apps.promotions.models import Discount


def _invalidate_on_discount_change():
    """Bump the effective-price generation and clear bundle price caches.

    A discount change can affect any product and any bundle, so both caches
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


def _product_price_changed(instance):
    """Return whether a product save changes its price.

    Fired from ``pre_save``, where the database still holds the prior row: a
    new product or a row whose stored price differs from the instance's price
    is a change that must refresh cached prices.

    Args:
        instance (Product): the product being saved.

    Returns:
        bool: True when the price changed or the product is new.
    """
    if instance.pk is None:
        return True
    previous = (
        Product.objects.filter(pk=instance.pk).values_list("price", flat=True).first()
    )
    return previous != instance.price


@receiver(pre_save, sender=Product)
def on_product_saved(sender, instance, **kwargs):
    """Invalidate effective prices when a product's price is about to change.

    Bundle price invalidation on a product change is handled by the bundles
    app's own signals, which already track which bundles reference the
    product; here only the generation is bumped so effective prices refresh.
    """
    if _product_price_changed(instance):
        promotions_cache.bump_discount_generation()


@receiver(post_delete, sender=Product)
def on_product_deleted(sender, instance, **kwargs):
    """Invalidate effective prices when a product is removed."""
    promotions_cache.bump_discount_generation()
