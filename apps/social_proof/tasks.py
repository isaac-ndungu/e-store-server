"""Celery tasks for the social proof app.

One scheduled task keeps the durable view-event table bounded: rows older than
the retention window are purged on a daily basis. The task is idempotent and
safe to run twice — deleting rows that no longer exist is a harmless no-op, and
a redelivery merely deletes a (possibly empty) older set.
"""

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.social_proof.constants import VIEW_EVENT_RETENTION_DAYS
from apps.social_proof.models import ProductViewEvent


@shared_task
def purge_old_view_events_task():
    """Delete durable view-event rows older than the retention window.

    Keeps the analytics table bounded as views accumulate. Idempotent: the
    delete targets a fixed age threshold, so rerunning it shortly after a
    success removes nothing.

    Returns:
        int: the number of rows deleted.
    """
    cutoff = timezone.now() - timedelta(days=VIEW_EVENT_RETENTION_DAYS)
    deleted_count, _ = ProductViewEvent.objects.filter(created_at__lt=cutoff).delete()
    return deleted_count
