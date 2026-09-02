"""Signal handlers that invalidate the collection membership cache.

Wired from ``apps.collections.apps.CollectionsConfig.ready``. Whenever a
collection or one of its membership rows is saved or deleted, the cached
product id list for that collection is dropped so the storefront never serves
a stale list. The invalidation is name-based via the cache helper rather
than a time-based TTL, so a changed slug, a repoint of membership, or a smart
refresh is reflected immediately on the next read.
"""

from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from apps.collections import cache
from apps.collections.models import Collection, CollectionMembership


@receiver(pre_save, sender=Collection)
def invalidate_collection_cache_on_save(sender, instance, **kwargs):
    """Drop the cached product list around a collection save.

    Runs before the row is written so the previous slug (in the case of a
    rename) is still available to purge; after the save the new slug is
    invalidated too. Deleting a cache key that does not exist is a no-op.

    Args:
        sender: the model class.
        instance (Collection): the collection being saved.
    """
    if instance.pk is not None:
        try:
            previous = Collection.objects.get(pk=instance.pk)
        except Collection.DoesNotExist:
            return
        cache.invalidate_collection(previous.slug)
    cache.invalidate_collection(instance.slug)


@receiver(pre_delete, sender=Collection)
def invalidate_collection_cache_on_delete(sender, instance, **kwargs):
    """Drop a deleted collection's cached product list.

    Args:
        sender: the model class.
        instance (Collection): the collection being deleted.
    """
    cache.invalidate_collection(instance.slug)


@receiver(pre_save, sender=CollectionMembership)
def invalidate_membership_cache_on_save(sender, instance, **kwargs):
    """Drop the parent collection's cached list when a membership changes.

    Args:
        sender: the model class.
        instance (CollectionMembership): the membership being saved.
    """
    cache.invalidate_collection(instance.collection.slug)


@receiver(pre_delete, sender=CollectionMembership)
def invalidate_membership_cache_on_delete(sender, instance, **kwargs):
    """Drop the parent collection's cached list when a membership is removed.

    Args:
        sender: the model class.
        instance (CollectionMembership): the membership being deleted.
    """
    cache.invalidate_collection(instance.collection.slug)
