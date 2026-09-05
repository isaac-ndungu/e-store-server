"""Business logic for the content app.

The service layer is the only place ``ContentPage`` and ``Banner`` rows are
created, updated, or deleted. All free-form text is sanitised with ``bleach``
at this boundary, so embedded scripts, iframes, and inline event handlers are
stripped before the content is stored and rendered on the storefront.

No feature flag gates content creation — unlike reviews or social proof,
content management is a core staff capability that is always enabled.
"""

import bleach
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.content.models import Banner, ContentPage

_ALLOWABLE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "code",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_ALLOWABLE_ATTRS = {
    "a": {"href", "title", "rel", "target"},
    "img": {"src", "alt", "width", "height"},
}

_ALLOWABLE_PROTOCOLS = {"http", "https", "mailto"}


def _sanitize_html(value):
    """Strip dangerous markup from staff-authored HTML content.

    Allows a curated set of structural and formatting tags while removing
    ``<script>``, ``<iframe>``, ``<object>``, inline event handlers, and
    other injection vectors.

    Args:
        value (str): the raw HTML text submitted by staff.

    Returns:
        str: the sanitised HTML.
    """
    return bleach.clean(
        value,
        tags=_ALLOWABLE_TAGS,
        attributes=_ALLOWABLE_ATTRS,
        protocols=_ALLOWABLE_PROTOCOLS,
        strip=True,
    )


def _sanitize_plain(value):
    """Strip all markup from plain-text fields.

    Used for titles, meta fields, and link URLs where no HTML is expected.

    Args:
        value (str): the raw text.

    Returns:
        str: the sanitised text.
    """
    return bleach.clean(value, tags=set(), strip=True)


def create_page(
    *, title, slug, body, is_published=True, meta_title="", meta_description=""
):
    """Create a new content page.

    The body is sanitised before storage to prevent XSS. Slug uniqueness is
    enforced by the database constraint and surfaces as a clean validation
    error rather than an integrity violation.

    Args:
        title (str): the page heading.
        slug (str): the URL-safe identifier.
        body (str): the HTML body content.
        is_published (bool): whether the page is storefront-visible.
        meta_title (str): optional SEO title override.
        meta_description (str): optional SEO description.

    Returns:
        ContentPage: the created page.

    Raises:
        ValidationError: when the slug is already in use.
    """
    page = ContentPage(
        title=_sanitize_plain(title),
        slug=slug,
        body=_sanitize_html(body),
        is_published=is_published,
        meta_title=_sanitize_plain(meta_title),
        meta_description=_sanitize_plain(meta_description),
    )
    try:
        page.save()
    except IntegrityError as exc:
        raise ValidationError(
            {"slug": "A page with this slug already exists."}
        ) from exc
    return page


def update_page(*, page, **fields):
    """Update a content page's fields.

    Every text field passed is sanitised before the write. Only fields
    explicitly included in ``fields`` are touched; omitted fields keep their
    current values.

    Args:
        page (ContentPage): the page to update.
        **fields: keyword arguments matching model field names.

    Returns:
        ContentPage: the updated page.

    Raises:
        ValidationError: when a new slug conflicts with an existing page.
    """
    updatable = {
        "title",
        "slug",
        "body",
        "is_published",
        "meta_title",
        "meta_description",
    }
    sanitise_html = {"body"}
    sanitise_plain = {"title", "meta_title", "meta_description"}
    update_fields = []

    for key, value in fields.items():
        if key not in updatable:
            continue
        if key in sanitise_html:
            value = _sanitize_html(value)
        elif key in sanitise_plain:
            value = _sanitize_plain(value)
        setattr(page, key, value)
        update_fields.append(key)

    if not update_fields:
        return page

    try:
        page.save(update_fields=update_fields)
    except IntegrityError as exc:
        raise ValidationError(
            {"slug": "A page with this slug already exists."}
        ) from exc
    return page


def delete_page(*, page):
    """Delete a content page.

    Args:
        page (ContentPage): the page to delete.

    Returns:
        None
    """
    page.delete()


def create_banner(
    *,
    title="",
    image,
    link_url="",
    placement,
    sort_order=0,
    starts_at=None,
    ends_at=None,
    is_active=True,
):
    """Create a promotional banner.

    The title is sanitised before storage. The image is validated by DRF's
    serializer before reaching this layer.

    Args:
        title (str): an optional banner label (not rendered publicly, used
            for staff identification).
        image: the uploaded image file.
        link_url (str): optional click-through URL.
        placement (str): the layout region key.
        sort_order (int): display ordering within the placement.
        starts_at: optional start of the display window.
        ends_at: optional end of the display window.
        is_active (bool): whether the banner is eligible for display.

    Returns:
        Banner: the created banner.
    """
    return Banner.objects.create(
        title=_sanitize_plain(title),
        image=image,
        link_url=link_url,
        placement=placement,
        sort_order=sort_order,
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=is_active,
    )


def update_banner(*, banner, **fields):
    """Update a banner's fields.

    Only fields explicitly included in ``fields`` are touched.

    Args:
        banner (Banner): the banner to update.
        **fields: keyword arguments matching model field names.

    Returns:
        Banner: the updated banner.
    """
    updatable = {
        "title",
        "image",
        "link_url",
        "placement",
        "sort_order",
        "starts_at",
        "ends_at",
        "is_active",
    }
    update_fields = []

    for key, value in fields.items():
        if key not in updatable:
            continue
        if key == "title":
            value = _sanitize_plain(value)
        setattr(banner, key, value)
        update_fields.append(key)

    if update_fields:
        banner.save(update_fields=update_fields)
    return banner


def delete_banner(*, banner):
    """Delete a banner.

    Args:
        banner (Banner): the banner to delete.

    Returns:
        None
    """
    banner.delete()
