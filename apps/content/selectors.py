"""Read-only query helpers for the content app.

Selectors encapsulate query construction so views never build raw querysets
directly. Public storefront reads filter by active/published status and use
``only()`` where appropriate; admin reads return full rows for the staff
management interface.
"""

from django.utils import timezone

from apps.content.models import Banner, ContentPage


def get_published_page_by_slug(slug):
    """Return a published content page by slug, or None.

    The full stored body is loaded because the caller renders it; the
    ``is_published`` filter keeps unpublished drafts from ever leaking into
    the public read.

    Args:
        slug (str): the page slug.

    Returns:
        ContentPage | None: the published page, or None when missing or
            unpublished.
    """
    return ContentPage.objects.filter(slug=slug, is_published=True).first()


def get_page_for_staff(page_id):
    """Return a single content page for staff management by id, or None.

    Args:
        page_id (int): the page primary key.

    Returns:
        ContentPage | None: the page, or None when no page matches.
    """
    return ContentPage.objects.filter(pk=page_id).first()


def list_all_pages(*, published=None):
    """Return content pages for the staff management list.

    Args:
        published (bool | None): when not None, restrict to pages with that
            publication state.

    Returns:
        QuerySet: pages ordered by title.
    """
    queryset = ContentPage.objects.order_by("title", "pk")
    if published is not None:
        queryset = queryset.filter(is_published=published)
    return queryset


def list_active_banners_for_placement(placement):
    """Return banners active right now for a given placement, sorted for display.

    A banner qualifies when ``is_active`` is True and the current time falls
    within its ``starts_at``/``ends_at`` window (or either bound is unset).

    Args:
        placement (str): the placement key (e.g. ``"homepage_hero"``).

    Returns:
        QuerySet: matching banners ordered by ``sort_order``.
    """
    now = timezone.now()
    return (
        Banner.objects.filter(
            is_active=True,
            placement=placement,
        )
        .filter(_schedule_contains(now))
        .order_by("sort_order", "pk")
    )


def _schedule_contains(now):
    """Build a Q object matching banners whose schedule contains ``now``.

    A banner with ``starts_at`` set must have started; one with ``ends_at``
    set must not have ended yet. Either bound may be null (no constraint).

    Args:
        now: the current datetime.

    Returns:
        Q: the combined schedule filter.
    """
    from django.db.models import Q

    starts = Q(starts_at__isnull=True) | Q(starts_at__lte=now)
    ends = Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    return starts & ends


def get_banner_for_staff(banner_id):
    """Return a single banner for staff management by id, or None.

    Args:
        banner_id (int): the banner primary key.

    Returns:
        Banner | None: the banner, or None when no banner matches.
    """
    return Banner.objects.filter(pk=banner_id).first()


def list_all_banners(*, active=None, placement=None):
    """Return banners for the staff management list.

    Args:
        active (bool | None): when not None, restrict to banners with that
            active state.
        placement (str | None): when not None, restrict to that placement.

    Returns:
        QuerySet: banners ordered by placement and sort_order.
    """
    queryset = Banner.objects.order_by("placement", "sort_order", "pk")
    if active is not None:
        queryset = queryset.filter(is_active=active)
    if placement is not None:
        queryset = queryset.filter(placement=placement)
    return queryset
