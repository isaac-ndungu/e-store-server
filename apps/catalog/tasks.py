"""Background tasks for the catalog app."""

from celery import shared_task

from apps.catalog import images
from apps.catalog.models import ProductImage


@shared_task
def generate_product_image_variants(image_id, storage_name=None):
    """Generate responsive variants for a product image.

    Idempotent and safe to redeliver: it returns early when the image's
    ``image_sources`` is already populated, so a retried task does not
    duplicate files or reset metadata. A source that fails to decode yields
    no variants, and the next delivery retries.

    Args:
        image_id (int): the ProductImage primary key.
        storage_name (str | None): the original image storage key, passed
            so the task still has the path even if the row is re-fetched.

    Returns:
        int: the number of variants generated.
    """
    try:
        product_image = ProductImage.objects.get(pk=image_id)
    except ProductImage.DoesNotExist:
        return 0

    if product_image.image_sources:
        return len(product_image.image_sources)

    sources = images.generate_variants(storage_name or product_image.image.name)
    if not sources:
        return 0

    product_image.image_sources = sources
    product_image.save(update_fields=["image_sources"])
    return len(sources)
