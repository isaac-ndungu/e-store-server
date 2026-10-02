"""Signal handlers that keep the category tree and facet caches coherent.

The storefront category list and detail payloads depend on the category
rows themselves and on each category's live product count. Any category
save/delete changes the tree, and any product save/delete can change a
category's product count, so all four events bump the category generation
to invalidate every cached read at once.

Facet counts are cached per filter combination under a facet generation.
Facet definitions change rarely but product edits change the counts, so
facet definition and product saves/deletes bump the facet generation.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.catalog import cache as catalog_cache
from apps.catalog.models import Category, FacetDefinition, Product


@receiver(post_save, sender=Category)
def on_category_saved(sender, instance, **kwargs):
    """Invalidate cached category reads when a category changes."""
    catalog_cache.bump_category_generation()


@receiver(post_delete, sender=Category)
def on_category_deleted(sender, instance, **kwargs):
    """Invalidate cached category reads when a category is deleted."""
    catalog_cache.bump_category_generation()


@receiver(post_save, sender=Product)
def on_product_saved(sender, instance, **kwargs):
    """Invalidate cached category and facet reads when a product changes.

    A product's category, active flag, or deletion alters one or more
    categories' product counts and can change facet aggregates.
    """
    catalog_cache.bump_category_generation()
    catalog_cache.bump_facet_generation()


@receiver(post_delete, sender=Product)
def on_product_deleted(sender, instance, **kwargs):
    """Invalidate cached category and facet reads when a product is deleted."""
    catalog_cache.bump_category_generation()
    catalog_cache.bump_facet_generation()


@receiver(post_save, sender=FacetDefinition)
def on_facet_definition_saved(sender, instance, **kwargs):
    """Invalidate cached facet counts when a facet definition changes."""
    catalog_cache.bump_facet_generation()


@receiver(post_delete, sender=FacetDefinition)
def on_facet_definition_deleted(sender, instance, **kwargs):
    """Invalidate cached facet counts when a facet definition is deleted."""
    catalog_cache.bump_facet_generation()
