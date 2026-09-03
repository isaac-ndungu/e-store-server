from apps.promotions.models import Coupon, Discount


def list_discounts(active_only=False):
    """Return discounts for public or admin listing.

    Args:
        active_only (bool): when True, only currently active discounts are
            returned.

    Returns:
        QuerySet: discounts in display order.
    """
    queryset = Discount.objects.all()
    if active_only:
        queryset = queryset.filter(is_active=True)
    return queryset


def list_coupons(active_only=False):
    """Return coupons for public or admin listing.

    Args:
        active_only (bool): when True, only currently active coupons are
            returned.

    Returns:
        QuerySet: coupons in display order.
    """
    queryset = Coupon.objects.all()
    if active_only:
        queryset = queryset.filter(is_active=True)
    return queryset
