"""Business logic for the content app.

The service layer is the only place ``ContentPage`` and ``Banner`` rows are
created, updated, or deleted. All free-form text is sanitised with ``bleach``
at this boundary, so embedded scripts, iframes, and inline event handlers are
stripped before the content is stored and rendered on the storefront.

No feature flag gates content creation  -  unlike reviews or social proof,
content management is a core staff capability that is always enabled.
"""

import html

import bleach
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.content.models import Banner, BlogCategory, BlogPost, BlogTag, ContentPage

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
    Entities are decoded after stripping because these values are escaped
    again at render time - storing them escaped would double-escape
    characters like ``&`` in titles.

    Args:
        value (str): the raw text.

    Returns:
        str: the sanitised text.
    """
    return html.unescape(bleach.clean(value, tags=set(), strip=True))


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


def _compute_reading_stats(html_body):
    """Derive word count and reading time from stored HTML.

    Tags are stripped before counting so markup never inflates the total.
    Reading time assumes 200 words per minute with a one-minute floor for
    any non-empty post.

    Args:
        html_body (str): the sanitised post body.

    Returns:
        tuple: ``(word_count, reading_minutes)``.
    """
    text = bleach.clean(html_body, tags=set(), strip=True)
    words = [part for part in text.split() if part.strip()]
    word_count = len(words)
    reading_minutes = max(1, (word_count + 199) // 200) if word_count else 0
    return word_count, reading_minutes


def create_blog_category(
    *, name, slug, description="", meta_title="", meta_description="", is_active=True
):
    """Create a blog category.

    Args:
        name (str): the category label.
        slug (str): the URL-safe identifier.
        description (str): archive page copy.
        meta_title (str): optional SEO title override.
        meta_description (str): optional SEO description.
        is_active (bool): whether the category is storefront-visible.

    Returns:
        BlogCategory: the created category.

    Raises:
        ValidationError: when the slug is already in use.
    """
    category = BlogCategory(
        name=_sanitize_plain(name),
        slug=slug,
        description=_sanitize_html(description),
        meta_title=_sanitize_plain(meta_title),
        meta_description=_sanitize_plain(meta_description),
        is_active=is_active,
    )
    try:
        category.save()
    except IntegrityError as exc:
        raise ValidationError(
            {"slug": "A category with this slug already exists."}
        ) from exc
    return category


def update_blog_category(*, category, **fields):
    """Update a blog category's fields.

    Args:
        category (BlogCategory): the category to update.
        **fields: keyword arguments matching model field names.

    Returns:
        BlogCategory: the updated category.

    Raises:
        ValidationError: when a new slug conflicts with an existing category.
    """
    updatable = {
        "name",
        "slug",
        "description",
        "meta_title",
        "meta_description",
        "sort_order",
        "is_active",
    }
    sanitise_html = {"description"}
    sanitise_plain = {"name", "meta_title", "meta_description"}
    update_fields = []

    for key, value in fields.items():
        if key not in updatable:
            continue
        if key in sanitise_html:
            value = _sanitize_html(value)
        elif key in sanitise_plain:
            value = _sanitize_plain(value)
        setattr(category, key, value)
        update_fields.append(key)

    if not update_fields:
        return category

    try:
        category.save(update_fields=update_fields)
    except IntegrityError as exc:
        raise ValidationError(
            {"slug": "A category with this slug already exists."}
        ) from exc
    return category


def delete_blog_category(*, category):
    """Delete a blog category.

    Posts in the category are kept with a null category.

    Args:
        category (BlogCategory): the category to delete.

    Returns:
        None
    """
    category.delete()


def create_blog_tag(*, name, slug):
    """Create a blog tag.

    Args:
        name (str): the tag label.
        slug (str): the URL-safe identifier.

    Returns:
        BlogTag: the created tag.

    Raises:
        ValidationError: when the slug is already in use.
    """
    tag = BlogTag(name=_sanitize_plain(name), slug=slug)
    try:
        tag.save()
    except IntegrityError as exc:
        raise ValidationError({"slug": "A tag with this slug already exists."}) from exc
    return tag


def delete_blog_tag(*, tag):
    """Delete a blog tag.

    Args:
        tag (BlogTag): the tag to delete.

    Returns:
        None
    """
    tag.delete()


def create_blog_post(
    *,
    title,
    slug,
    body,
    excerpt="",
    category=None,
    author_name="",
    status="draft",
    published_at=None,
    meta_title="",
    meta_description="",
    canonical_url="",
    noindex=False,
    cover_image=None,
    og_image=None,
    tag_ids=(),
    related_product_ids=(),
):
    """Create a blog post with its tag and product links.

    The body and plain-text fields are sanitised before storage and the
    reading stats are derived from the stored HTML, so the API never trusts
    caller-supplied counts. Images are validated by the serializer before
    reaching this layer.

    Args:
        title (str): the post heading.
        slug (str): the URL-safe identifier.
        body (str): the HTML article body.
        excerpt (str): short summary used for listings and meta fallback.
        category: an optional BlogCategory instance.
        author_name (str): display byline stored as text.
        status (str): ``draft`` or ``published``.
        published_at: optional publish timestamp for scheduling.
        meta_title (str): optional SEO title override.
        meta_description (str): optional SEO description.
        canonical_url (str): optional canonical override.
        noindex (bool): whether crawlers should skip this post.
        cover_image: optional uploaded cover image.
        og_image: optional uploaded social-share image override.
        tag_ids: iterable of BlogTag primary keys to attach.
        related_product_ids: iterable of Product primary keys to link.

    Returns:
        BlogPost: the created post.

    Raises:
        ValidationError: when the slug is already in use or a status is invalid.
    """
    if status not in ("draft", "published"):
        raise ValidationError({"status": "Status must be draft or published."})
    clean_body = _sanitize_html(body)
    word_count, reading_minutes = _compute_reading_stats(clean_body)
    post = BlogPost(
        title=_sanitize_plain(title),
        slug=slug,
        body=clean_body,
        excerpt=_sanitize_plain(excerpt),
        category=category,
        author_name=_sanitize_plain(author_name),
        status=status,
        published_at=published_at,
        meta_title=_sanitize_plain(meta_title),
        meta_description=_sanitize_plain(meta_description),
        canonical_url=canonical_url.strip(),
        noindex=noindex,
        word_count=word_count,
        reading_minutes=reading_minutes,
    )
    if cover_image is not None:
        post.cover_image = cover_image
    if og_image is not None:
        post.og_image = og_image
    try:
        post.save()
    except IntegrityError as exc:
        raise ValidationError(
            {"slug": "A post with this slug already exists."}
        ) from exc
    if tag_ids:
        post.tags.set(BlogTag.objects.filter(pk__in=list(tag_ids)))
    if related_product_ids:
        from apps.catalog.models import Product

        post.related_products.set(
            Product.objects.filter(pk__in=list(related_product_ids))
        )
    return post


def update_blog_post(*, post, tag_ids=None, related_product_ids=None, **fields):
    """Update a blog post's fields.

    Reading stats are recomputed whenever the body changes. Tag and product
    links are replaced wholesale when the corresponding id list is supplied
    and left untouched when omitted.

    Args:
        post (BlogPost): the post to update.
        tag_ids: optional replacement list of BlogTag primary keys.
        related_product_ids: optional replacement list of Product primary keys.
        **fields: keyword arguments matching model field names.

    Returns:
        BlogPost: the updated post.

    Raises:
        ValidationError: when a new slug conflicts, a status is invalid, or
            the slug of a post that stays published is changed. Published
            slugs are locked because live URLs are indexed and linked;
            unpublish first, then rename.
    """
    if (
        post.status == "published"
        and "slug" in fields
        and fields["slug"] != post.slug
        and fields.get("status", post.status) == "published"
    ):
        raise ValidationError(
            {
                "slug": (
                    "The slug of a published post cannot be changed. "
                    "Unpublish the post first, then rename it."
                )
            }
        )
    updatable = {
        "title",
        "slug",
        "body",
        "excerpt",
        "category",
        "author_name",
        "status",
        "published_at",
        "meta_title",
        "meta_description",
        "canonical_url",
        "noindex",
        "cover_image",
        "og_image",
    }
    sanitise_html = {"body"}
    sanitise_plain = {
        "title",
        "excerpt",
        "author_name",
        "meta_title",
        "meta_description",
    }
    update_fields = []

    for key, value in fields.items():
        if key not in updatable:
            continue
        if key == "status" and value not in ("draft", "published"):
            raise ValidationError({"status": "Status must be draft or published."})
        if key in sanitise_html:
            value = _sanitize_html(value)
        elif key in sanitise_plain:
            value = _sanitize_plain(value)
        elif key == "canonical_url" and value:
            value = value.strip()
        setattr(post, key, value)
        update_fields.append(key)

    if "body" in update_fields:
        post.word_count, post.reading_minutes = _compute_reading_stats(post.body)
        update_fields.extend(["word_count", "reading_minutes"])

    if update_fields:
        try:
            post.save(update_fields=list(dict.fromkeys(update_fields)))
        except IntegrityError as exc:
            raise ValidationError(
                {"slug": "A post with this slug already exists."}
            ) from exc

    if tag_ids is not None:
        post.tags.set(BlogTag.objects.filter(pk__in=list(tag_ids)))
    if related_product_ids is not None:
        from apps.catalog.models import Product

        post.related_products.set(
            Product.objects.filter(pk__in=list(related_product_ids))
        )
    return post


def delete_blog_post(*, post):
    """Delete a blog post.

    Args:
        post (BlogPost): the post to delete.

    Returns:
        None
    """
    post.delete()
