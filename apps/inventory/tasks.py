"""Background tasks for the inventory app."""

import logging

from celery import shared_task
from django.utils import timezone

from apps.inventory.models import StockReservation
from apps.inventory.services import release_reservation

logger = logging.getLogger(__name__)


@shared_task
def expire_stale_reservations():
    """Release active stock reservations that have passed their expiry.

    Idempotent and safe to redeliver: ``release_reservation`` is itself a
    no-op on a reservation already in a terminal state, so a retried sweep
    does not double-release. Catches per-row failures so one unexpected
    reservation does not block the rest of the sweep.

    Returns:
        int: the number of reservations released.
    """
    now = timezone.now()
    stale_pks = list(
        StockReservation.objects.filter(
            status="active", expires_at__lte=now
        ).values_list("pk", flat=True)
    )
    released = 0
    for pk in stale_pks:
        try:
            reservation = StockReservation.objects.get(pk=pk)
        except StockReservation.DoesNotExist:
            continue
        try:
            if release_reservation(reservation):
                released += 1
        except Exception:
            logger.exception("Failed to release expired reservation %s", pk)
    return released
