"""Data models for the content app.

Static content pages (about us, return policy, FAQs, etc.) and promotional
banners are managed through the staff API and rendered on the storefront.
Content pages are public by slug; banners are fetched by placement so the
storefront can slot them into predefined layout regions.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.http import url_has_allowed_host_and_scheme

from apps.catalog.validators import validate_image_upload


class ContentPage(models.Model):
    """A CMS-style static content page (e.g. about-us, return-policy, faq).

    ``slug`` is used for public storefront retrieval and must be unique.
    ``body`` is stored as raw HTML; staff-supplied markup is sanitised at the
    service boundary before persistence. ``is_published`` gates storefront
    visibility  -  drafts are hidden from public reads but visible in the staff
    moderation list.
    """

    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    body = models.TextField()
    is_published = models.BooleanField(default=True)
    meta_title = models.CharField(max_length=255, blank=True)
    meta_description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "pk"]
        indexes = [
            models.Index(
                fields=["is_published", "slug"],
                name="cp_published_slug_idx",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the page."""
        return f"content page: {self.title}"


class Banner(models.Model):
    """A promotional banner placed in a named layout region on the storefront.

    ``placement`` is a free-form string identifying where the banner renders
    (e.g. ``"homepage_hero"``, ``"category_top"``, ``"cart_upsell"``). The
    storefront queries banners by active status, placement, and sort order.

    ``image`` is required  -  banners without a visual asset are not useful.
    ``link_url`` is optional: when present it drives the click-through
    destination; when absent the banner is purely decorative.

    ``starts_at`` and ``ends_at`` allow time-boxed promotions; a banner
    without either date is always active (subject to ``is_active``).
    """

    title = models.CharField(max_length=255, blank=True)
    image = models.ImageField(
        upload_to="content/banners/", validators=[validate_image_upload]
    )
    link_url = models.CharField(max_length=500, blank=True)
    placement = models.CharField(max_length=50)
    sort_order = models.PositiveIntegerField(default=0)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["placement", "sort_order", "pk"]
        indexes = [
            models.Index(
                fields=["is_active", "placement"],
                name="banner_active_place_idx",
            ),
            models.Index(fields=["sort_order"], name="banner_sort_idx"),
        ]

    def clean(self):
        """Validate that a link URL only reaches http(s) or relative targets.

        Raises:
            ValidationError: if ``link_url`` uses an unsafe scheme.
        """
        super().clean()
        if not self.link_url:
            return
        if self.link_url.startswith("//"):
            raise ValidationError(
                {"link_url": "Protocol-relative URLs are not allowed."}
            )
        if not (
            self.link_url.startswith("/")
            or url_has_allowed_host_and_scheme(
                self.link_url,
                allowed_hosts=None,
                require_https=False,
            )
        ):
            raise ValidationError(
                {
                    "link_url": (
                        "link_url must be an http(s) URL or a relative path "
                        "starting with '/'."
                    )
                }
            )

    def __str__(self):
        """Return a compact label identifying the banner."""
        label = self.title or self.placement
        return f"banner: {label}"


class BlogCategory(models.Model):
    """A topical grouping for blog posts (e.g. buying guides, energy saving).

    Categories power archive pages and internal linking, so each one carries
    its own SEO title and description. Inactive categories are hidden from
    public reads but remain visible to staff.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    meta_title = models.CharField(max_length=255, blank=True)
    meta_description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name", "pk"]
        indexes = [
            models.Index(fields=["is_active", "slug"], name="bc_active_slug_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the category."""
        return f"blog category: {self.name}"


class BlogTag(models.Model):
    """A cross-cutting label for blog posts (e.g. energy-saving, hisense).

    Tags complement categories: a post lives in one category but can carry
    several tags, giving crawlers more topical paths to the same content.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name", "pk"]
        indexes = [
            models.Index(fields=["slug"], name="btag_slug_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the tag."""
        return f"blog tag: {self.name}"


class BlogPost(models.Model):
    """An SEO article that links editorial content to sellable products.

    Only rows with ``status`` set to published and ``published_at`` in the
    past are visible on the storefront; drafts and scheduled posts return 404
    on public reads. ``related_products`` is the commercial link: every guide
    points at real products so ranking content passes equity to product pages.
    The storefront renders ``body`` as stored, builds ``<title>`` from
    ``meta_title`` (falling back to ``title``), and injects an Article
    structured-data block from the detail payload.
    """

    STATUS_CHOICES = (("draft", "Draft"), ("published", "Published"))

    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    excerpt = models.CharField(max_length=500, blank=True)
    body = models.TextField()
    cover_image = models.ImageField(
        upload_to="content/blog/", blank=True, validators=[validate_image_upload]
    )
    category = models.ForeignKey(
        BlogCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts",
    )
    tags = models.ManyToManyField(BlogTag, blank=True, related_name="posts")
    related_products = models.ManyToManyField(
        "catalog.Product", blank=True, related_name="blog_posts"
    )
    author_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    meta_title = models.CharField(max_length=255, blank=True)
    meta_description = models.TextField(blank=True)
    canonical_url = models.CharField(max_length=500, blank=True)
    og_image = models.ImageField(
        upload_to="content/blog/og/", blank=True, validators=[validate_image_upload]
    )
    noindex = models.BooleanField(default=False)
    reading_minutes = models.PositiveIntegerField(default=0)
    word_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-pk"]
        indexes = [
            models.Index(
                fields=["status", "published_at"], name="bpost_status_pub_idx"
            ),
            models.Index(fields=["slug"], name="bpost_slug_idx"),
            models.Index(
                fields=["category", "status", "published_at"],
                name="bpost_cat_status_idx",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying the post."""
        return f"blog post: {self.title}"
