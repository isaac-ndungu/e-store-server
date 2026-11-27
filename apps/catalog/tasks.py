"""Background tasks for the catalog app."""

import json
import logging
import urllib.request

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from apps.catalog import images
from apps.catalog.models import Product, ProductImage

logger = logging.getLogger(__name__)

_STOREFRONT_REVALIDATE_LOCK_KEY = "catalog:faq_revalidate:{product_id}"
_STOREFRONT_REVALIDATE_LOCK_SECONDS = 30


def schedule_storefront_product_revalidation(product_id):
    """Queue one storefront product revalidation after the current transaction.

    The lock debounces the several row-level saves and deletes produced by one
    FAQ replacement or admin inline submission into a single request.
    """
    lock_key = _STOREFRONT_REVALIDATE_LOCK_KEY.format(product_id=product_id)

    def enqueue():
        try:
            if cache.add(lock_key, 1, timeout=_STOREFRONT_REVALIDATE_LOCK_SECONDS):
                revalidate_storefront_product.apply_async(
                    args=[product_id], countdown=2
                )
        except Exception:
            logger.warning(
                "could not queue storefront revalidation for product_id=%s",
                product_id,
                exc_info=True,
            )

    transaction.on_commit(enqueue)


@shared_task
def revalidate_storefront_product(product_id):
    """Ask the storefront to regenerate one product page after FAQ edits.

    Args:
        product_id (int): the product whose FAQ list changed.

    Returns:
        bool: whether the storefront accepted the revalidation request.
    """
    lock_key = _STOREFRONT_REVALIDATE_LOCK_KEY.format(product_id=product_id)
    try:
        slug = (
            Product.objects.filter(pk=product_id).values_list("slug", flat=True).first()
        )
        if slug is None:
            return False

        url = (settings.STOREFRONT_REVALIDATE_URL or "").strip()
        secret = (settings.STOREFRONT_REVALIDATE_SECRET or "").strip()
        if not url or not secret:
            return False

        request = urllib.request.Request(
            url,
            data=json.dumps({"slug": slug}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Revalidate-Secret": secret,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return 200 <= response.status < 300
    except Exception:
        logger.warning(
            "storefront revalidation failed for product_id=%s",
            product_id,
            exc_info=True,
        )
        return False
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            logger.warning(
                "could not release storefront revalidation lock for product_id=%s",
                product_id,
                exc_info=True,
            )


@shared_task
def generate_product_image_variants(image_id, storage_name=None, force=False):
    """Generate responsive variants for a product image.

    Idempotent and safe to redeliver: it returns early when the image's
    ``image_sources`` is already populated unless ``force`` is true. Variant
    names include a source fingerprint, so forced regeneration does not
    overwrite files still referenced by existing responses.

    Args:
        image_id (int): the ProductImage primary key.
        storage_name (str | None): the original image storage key, passed
            so the task still has the path even if the row is re-fetched.
        force (bool): whether to regenerate variants for an already processed
            image.

    Returns:
        int: the number of variants generated.
    """
    try:
        product_image = ProductImage.objects.get(pk=image_id)
    except ProductImage.DoesNotExist:
        return 0

    if product_image.image_sources and not force:
        return len(product_image.image_sources)

    sources = images.generate_variants(storage_name or product_image.image.name)
    if not sources:
        return 0

    if sources != product_image.image_sources:
        product_image.image_sources = sources
        product_image.save(update_fields=["image_sources"])
    return len(sources)
