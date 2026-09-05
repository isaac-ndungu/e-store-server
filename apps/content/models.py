"""Data models for the content app.

Static content pages (about us, return policy, FAQs, etc.) and promotional
banners are managed through the staff API and rendered on the storefront.
Content pages are public by slug; banners are fetched by placement so the
storefront can slot them into predefined layout regions.
"""

from django.db import models


class ContentPage(models.Model):
    """A CMS-style static content page (e.g. about-us, return-policy, faq).

    ``slug`` is used for public storefront retrieval and must be unique.
    ``body`` is stored as raw HTML; staff-supplied markup is sanitised at the
    service boundary before persistence. ``is_published`` gates storefront
    visibility — drafts are hidden from public reads but visible in the staff
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

    ``image`` is required — banners without a visual asset are not useful.
    ``link_url`` is optional: when present it drives the click-through
    destination; when absent the banner is purely decorative.

    ``starts_at`` and ``ends_at`` allow time-boxed promotions; a banner
    without either date is always active (subject to ``is_active``).
    """

    title = models.CharField(max_length=255, blank=True)
    image = models.ImageField(upload_to="content/banners/")
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
                fields=["is_active", "placement", "sort_order"],
                name="bn_active_placement_idx",
            ),
            models.Index(fields=["starts_at", "ends_at"], name="bn_schedule_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying the banner."""
        label = self.title or self.placement
        return f"banner: {label}"
