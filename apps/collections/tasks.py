"""Background tasks for the collections app."""

import logging

from celery import shared_task

from apps.collections.services import refresh_all_smart_collections

logger = logging.getLogger(__name__)


@shared_task
def refresh_smart_collections():
    """Recompute membership for every in-window smart collection.

    Idempotent and safe to redeliver: each refresh deletes then recreates the
    membership rows for a collection inside a transaction, so a retried sweep
    converges on the same membership. The product lists are cached per slug
    and dropped by the membership signals whenever a refresh rewrites them.

    Returns:
        int: the number of smart collections refreshed.
    """
    try:
        return refresh_all_smart_collections()
    except Exception:
        logger.exception("Smart-collection refresh failed")
        return 0
