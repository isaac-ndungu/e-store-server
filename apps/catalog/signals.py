"""Signal handlers that keep the category tree cache coherent.

The storefront category list and detail payloads depend on the category
rows themselves and on each category's live product count. Any category
save/delete changes the tree, and any product save/delete can change a
category's product count, so all four events bump the category generation
to invalidate every cached read at once.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.catalog import cache as catalog_cache
from apps.catalog.models import Category, Product


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
    """Invalidate cached category reads when a product changes.

    A product's category, active flag, or deletion alters one or more
    categories' product counts.
    """
    catalog_cache.bump_category_generation()


@receiver(post_delete, sender=Product)
def on_product_deleted(sender, instance, **kwargs):
    """Invalidate cached category reads when a product is deleted."""
    catalog_cache.bump_category_generation()
