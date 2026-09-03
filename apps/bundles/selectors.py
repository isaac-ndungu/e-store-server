from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.bundles.models import Bundle, BundleItem


def _bundles_enabled():
    """Return True when the bundles feature is on for the storefront.

    Reads the ``enable_bundles`` toggle from ``SiteConfig``, defaulting to
    ``True`` on a fresh install where the settings JSON hasn't been edited.

    Returns:
        bool: whether bundles are enabled.
    """
    from apps.core.models import SiteConfig

    return SiteConfig.load().settings.get("enable_bundles", True)


def _in_window_filter():
    """Return the condition for bundles offered to the storefront right now.

    A bundle is live when it has no configured ``starts_at`` / ``ends_at`` or
    when ``now`` falls inside the window. Expressed as two nullable comparisons
    so a missing bound never excludes the bundle.

    Returns:
        Q: the filter object.
    """
    now = timezone.now()
    return (Q(starts_at__isnull=True) | Q(starts_at__lte=now)) & (
        Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    )


def _with_items(queryset):
    """Attach a pre-fetched item list to a bundle queryset.

    Args:
        queryset (QuerySet): the bundle queryset.

    Returns:
        QuerySet: the queryset with bundle items pre-fetched.
    """
    return queryset.prefetch_related(
        Prefetch(
            "items",
            queryset=BundleItem.objects.select_related("product", "variant").order_by(
                "pk"
            ),
        )
    )


def list_bundles(active_only=False):
    """Return bundles for public or admin listing.

    When ``active_only`` is ``True`` (public storefront), an empty queryset
    is returned if the ``enable_bundles`` feature flag is off. Admin callers
    always see all bundles regardless of the flag.

    Args:
        active_only (bool): when True, only bundles offered to the storefront
            are returned.

    Returns:
        QuerySet: bundles in display order.
    """
    queryset = Bundle.objects.all()
    if active_only and not _bundles_enabled():
        return queryset.none()
    if active_only:
        queryset = queryset.filter(is_active=True).filter(_in_window_filter())
    return queryset


def get_bundle_by_slug(slug, active_only=False):
    """Return a single bundle by slug, or None.

    When ``active_only`` is ``True`` (public storefront), ``None`` is
    returned if the ``enable_bundles`` feature flag is off.

    Args:
        slug (str): the bundle slug.
        active_only (bool): when True, inactive or out-of-window bundles are
            not returned.

    Returns:
        Bundle | None: the bundle with items pre-fetched, or None.
    """
    queryset = _with_items(Bundle.objects.filter(slug=slug))
    if active_only and not _bundles_enabled():
        return None
    if active_only:
        queryset = queryset.filter(is_active=True).filter(_in_window_filter())
    return queryset.first()
