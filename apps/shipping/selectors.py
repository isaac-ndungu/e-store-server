"""Selectors for the shipping app.

Read-only query helpers for listing delivery zones and per-zone warehouse
priorities. Views delegate here so the query patterns (filters, related
prefetching, ordering) live in one place and stay consistent.
"""

from apps.shipping.models import DeliveryZone, WarehouseZonePriority


def list_delivery_zones(active_only=False, county=None):
    """Return delivery zones for public or admin listing.

    Args:
        active_only (bool): when True, only zones offered to the storefront
            are returned.
        county (str | None): optional county filter.

    Returns:
        QuerySet: zones with any related data pre-fetched, ordered.
    """
    queryset = DeliveryZone.objects.all()
    if active_only:
        queryset = queryset.filter(is_active=True)
    if county is not None:
        queryset = queryset.filter(county=county)
    return queryset


def list_zone_priorities(delivery_zone_id=None):
    """Return warehouse-priority rows for admin listing.

    Args:
        delivery_zone_id (int | None): optional zone filter.

    Returns:
        QuerySet: priority rows with the warehouse pre-fetched.
    """
    queryset = WarehouseZonePriority.objects.select_related(
        "delivery_zone", "warehouse"
    )
    if delivery_zone_id is not None:
        queryset = queryset.filter(delivery_zone_id=delivery_zone_id)
    return queryset
