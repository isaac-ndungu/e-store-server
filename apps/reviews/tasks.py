"""Asynchronous background work for the reviews app.

Keeps customer uploads honest: a photo that is never attached to a review has
no customer-facing way to be cleaned up afterward, so a scheduled task removes
the orphans once they age past the configured TTL.
"""

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.reviews.constants import ORPHAN_PHOTO_TTL_HOURS
from apps.reviews.models import ReviewPhoto


@shared_task
def cleanup_orphan_review_photos():
    """Delete review photos never attached to a review along with their files.

    Deletes uploads orphaned for longer than the configured TTL. Deleting each
    row fires the photo's ``post_delete`` signal, which removes the stored
    files. Idempotent and safe to retry: a repeated run finds no further
    orphans, and deleting files that no longer exist is a no-op on the storage
    backend.

    Returns:
        int: the number of orphaned photos deleted.
    """
    threshold = timezone.now() - timedelta(hours=ORPHAN_PHOTO_TTL_HOURS)
    deleted, _ = ReviewPhoto.objects.filter(
        review__isnull=True,
        created_at__lt=threshold,
    ).delete()
    return deleted
