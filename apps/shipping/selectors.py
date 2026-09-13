"""Selectors for the shipping app.

Read-only query helpers for listing delivery areas. Views delegate here so
the query patterns (filters, ordering) live in one place and stay
consistent.
"""

from apps.shipping.models import DeliveryArea


def list_delivery_areas(active_only=False, county=None):
    """Return delivery areas for public or admin listing.

    Args:
        active_only (bool): when True, only areas offered to the
            storefront are returned.
        county (str | None): optional county filter.

    Returns:
        QuerySet: areas ordered by county then area name.
    """
    queryset = DeliveryArea.objects.all()
    if active_only:
        queryset = queryset.filter(is_active=True)
    if county is not None:
        queryset = queryset.filter(county=county)
    return queryset
