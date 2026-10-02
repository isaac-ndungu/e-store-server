"""Signal handlers that keep the category tree, facet, and list caches coherent.

The storefront category list and detail payloads depend on the category
rows themselves and on each category's live product count. Any category
save/delete changes the tree, and any product save/delete can change a
category's product count, so all four events bump the category generation
to invalidate every cached read at once.

Facet counts are cached per filter combination under a facet generation.
Facet definitions change rarely but product edits change the counts, so
facet definition and product saves/deletes bump the facet generation.

The cached product-list pages embed product rows, primary-image URLs, and
facet aggregates, so product, category, product-image, and facet
definition saves/deletes bump the product-list generation as well.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.catalog import cache as catalog_cache
from apps.catalog.models import Category, FacetDefinition, Product, ProductImage


@receiver(post_save, sender=Category)
def on_category_saved(sender, instance, **kwargs):
    """Invalidate cached category and product-list reads on category change."""
    catalog_cache.bump_category_generation()
    catalog_cache.bump_product_list_generation()


@receiver(post_delete, sender=Category)
def on_category_deleted(sender, instance, **kwargs):
    """Invalidate cached category and product-list reads on category delete."""
    catalog_cache.bump_category_generation()
    catalog_cache.bump_product_list_generation()


@receiver(post_save, sender=Product)
def on_product_saved(sender, instance, **kwargs):
    """Invalidate cached category, facet, and product-list reads on change.

    A product's category, active flag, or deletion alters one or more
    categories' product counts and can change facet aggregates.
    """
    catalog_cache.bump_category_generation()
    catalog_cache.bump_facet_generation()
    catalog_cache.bump_product_list_generation()


@receiver(post_delete, sender=Product)
def on_product_deleted(sender, instance, **kwargs):
    """Invalidate cached category, facet, and product-list reads on delete."""
    catalog_cache.bump_category_generation()
    catalog_cache.bump_facet_generation()
    catalog_cache.bump_product_list_generation()


@receiver(post_save, sender=ProductImage)
def on_product_image_saved(sender, instance, **kwargs):
    """Invalidate cached product-list pages when an image changes.

    List cards render the primary image URL, so a new, replaced, or
    re-flagged image must retire cached pages.
    """
    catalog_cache.bump_product_list_generation()


@receiver(post_delete, sender=ProductImage)
def on_product_image_deleted(sender, instance, **kwargs):
    """Invalidate cached product-list pages when an image is deleted."""
    catalog_cache.bump_product_list_generation()


@receiver(post_save, sender=FacetDefinition)
def on_facet_definition_saved(sender, instance, **kwargs):
    """Invalidate cached facet counts and product-list pages on change."""
    catalog_cache.bump_facet_generation()
    catalog_cache.bump_product_list_generation()


@receiver(post_delete, sender=FacetDefinition)
def on_facet_definition_deleted(sender, instance, **kwargs):
    """Invalidate cached facet counts and product-list pages on delete."""
    catalog_cache.bump_facet_generation()
    catalog_cache.bump_product_list_generation()
