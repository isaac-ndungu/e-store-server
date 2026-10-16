"""Read-only query helpers for the content app.

Selectors encapsulate query construction so views never build raw querysets
directly. Public storefront reads filter by active/published status and use
``only()`` where appropriate; admin reads return full rows for the staff
management interface.
"""

from django.utils import timezone

from apps.content.models import Banner, BlogCategory, BlogPost, BlogTag, ContentPage


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


def list_active_blog_categories():
    """Return active blog categories with their published post counts.

    Returns:
        QuerySet: categories ordered for archive display.
    """
    from django.db.models import Count, Q

    now = timezone.now()
    return (
        BlogCategory.objects.filter(is_active=True)
        .annotate(
            post_count=Count(
                "posts",
                filter=Q(posts__status="published") & Q(posts__published_at__lte=now),
            )
        )
        .order_by("sort_order", "name", "pk")
    )


def get_blog_category_by_slug(slug):
    """Return an active blog category by slug, or None.

    Args:
        slug (str): the category slug.

    Returns:
        BlogCategory | None: the category, or None when missing or inactive.
    """
    return BlogCategory.objects.filter(slug=slug, is_active=True).first()


def list_blog_tags():
    """Return all blog tags ordered by name.

    Returns:
        QuerySet: tags ordered by name.
    """
    return BlogTag.objects.order_by("name", "pk")


def list_published_posts(
    *,
    category_slug=None,
    tag_slug=None,
    search=None,
    product_id=None,
    exclude_noindex=False,
):
    """Return published blog posts with relations prefetched for listing.

    Only posts with ``status`` published and ``published_at`` in the past
    are included. The category join and tag/product prefetches keep the list
    view to a constant query count.

    Args:
        category_slug (str | None): restrict to this category slug.
        tag_slug (str | None): restrict to posts carrying this tag slug.
        search (str | None): case-insensitive match on title and excerpt.
        product_id (int | None): restrict to posts linking this product,
            used for related-guide strips on product pages.
        exclude_noindex (bool): skip posts staff flagged as hidden from
            search engines. Used by crawler feeds, which must never
            advertise a noindex URL.

    Returns:
        QuerySet: matching posts newest first.
    """
    now = timezone.now()
    queryset = (
        BlogPost.objects.filter(status="published", published_at__lte=now)
        .select_related("category")
        .prefetch_related("tags", "related_products")
        .order_by("-published_at", "-pk")
    )
    if category_slug:
        queryset = queryset.filter(category__slug=category_slug)
    if tag_slug:
        queryset = queryset.filter(tags__slug=tag_slug)
    if product_id:
        queryset = queryset.filter(related_products__pk=product_id)
    if exclude_noindex:
        queryset = queryset.filter(noindex=False)
    if search:
        from django.db.models import Q

        queryset = queryset.filter(
            Q(title__icontains=search) | Q(excerpt__icontains=search)
        )
    return queryset


def get_published_post_by_slug(slug):
    """Return a published blog post by slug, or None.

    Drafts and scheduled (future-dated) posts are never exposed, and
    ``noindex`` posts stay reachable but flagged so the storefront can emit
    the robots meta tag.

    Args:
        slug (str): the post slug.

    Returns:
        BlogPost | None: the post, or None when missing or not yet public.
    """
    now = timezone.now()
    try:
        return (
            BlogPost.objects.select_related("category")
            .prefetch_related("tags", "related_products")
            .get(slug=slug, status="published", published_at__lte=now)
        )
    except BlogPost.DoesNotExist:
        return None


def list_related_posts(*, post, limit=3):
    """Return published posts related to the given post.

    Same-category posts rank first, then the newest published posts,
    excluding the post itself.

    Args:
        post (BlogPost): the reference post.
        limit (int): maximum posts to return.

    Returns:
        QuerySet: related published posts.
    """
    from django.db.models import Case, IntegerField, When

    now = timezone.now()
    queryset = BlogPost.objects.filter(
        status="published", published_at__lte=now
    ).exclude(pk=post.pk)
    if post.category_id:
        queryset = queryset.annotate(
            same_category=Case(
                When(category_id=post.category_id, then=1),
                default=0,
                output_field=IntegerField(),
            )
        ).order_by("-same_category", "-published_at", "-pk")
    else:
        queryset = queryset.order_by("-published_at", "-pk")
    return queryset[:limit]


def get_blog_post_for_staff(post_id):
    """Return a blog post for staff management by id, or None.

    Args:
        post_id (int): the post primary key.

    Returns:
        BlogPost | None: the post, or None when no post matches.
    """
    return BlogPost.objects.filter(pk=post_id).first()


def list_all_blog_posts(*, status=None):
    """Return blog posts for the staff management list.

    Args:
        status (str | None): when set, restrict to that publication state.

    Returns:
        QuerySet: posts ordered newest first.
    """
    queryset = BlogPost.objects.select_related("category").order_by(
        "-published_at", "-pk"
    )
    if status in ("draft", "published"):
        queryset = queryset.filter(status=status)
    return queryset
