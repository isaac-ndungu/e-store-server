"""Signal handlers that keep the reviews feature flag and aggregates coherent.

The ``enable_reviews`` toggle is cached so the per-request write gate is one
cache read; any change to the configuration singleton drops the cached value
or the flag could serve stale state until the TTL expires.

Deletions bypass the services layer entirely (administrator deletes, user
cascades, product cascades), so ``post_delete`` handlers keep the things the
services normally maintain — the product's rating aggregate and the stored
photo files — consistent on every way a review or photo can disappear.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.catalog.images import delete_image_files
from apps.catalog.models import Product
from apps.core.models import SiteConfig
from apps.reviews import cache as reviews_cache
from apps.reviews.models import Review, ReviewPhoto
from apps.reviews.services import _recompute_rating


@receiver(post_save, sender=SiteConfig)
def on_site_config_saved(sender, instance, **kwargs):
    """Drop the cached feature flag when the configuration row changes."""
    reviews_cache.invalidate_feature_enabled()


@receiver(post_delete, sender=Review)
def recompute_rating_on_review_deleted(sender, instance, **kwargs):
    """Keep the product's rating aggregate right when a review is deleted.

    Reviews can disappear outside the services layer — an administrator
    delete, or a cascade from the reviewer's account or the product — so the
    aggregate is recomputed here. When the product itself is being deleted the
    row is already gone and the recompute is skipped.
    """
    product = Product.objects.only("pk").filter(pk=instance.product_id).first()
    if product is not None:
        _recompute_rating(product.pk)


@receiver(post_delete, sender=ReviewPhoto)
def delete_photo_files(sender, instance, **kwargs):
    """Remove a photo's stored original and processed variants from storage.

    Called on every deletion path — review cascade, moderator photo delete, or
    the orphan sweep — so no review photo ever leaves orphaned files behind.
    """
    delete_image_files(instance.storage_name)
