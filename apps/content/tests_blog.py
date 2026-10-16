"""Tests for the blog endpoints in the content app.

Covers public storefront reads (list, detail, categories, tags, sitemap,
RSS), the staff-only CRUD paths, the access-control matrix (anonymous vs.
non-staff vs. staff), and the SEO boundaries: draft/scheduled visibility,
HTML sanitisation, and related-product linking.
"""

import datetime
from io import BytesIO

from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product
from apps.content.models import BlogCategory, BlogPost, BlogTag
from apps.content.services import create_blog_post, update_blog_post


def _make_user(email, username, is_staff=False):
    """Create a user for tests.

    Args:
        email (str): the login email.
        username (str): the username.
        is_staff (bool): whether the user can reach admin endpoints.

    Returns:
        User: the created user.
    """
    return User.objects.create_user(
        email=email,
        username=username,
        password="StrongPass123!",
        phone_number="+254700000099",
        is_staff=is_staff,
    )


def _login(client, email):
    """Attach a JWT access token to the test client.

    Args:
        client: the DRF test client.
        email (str): the login email.
    """
    url = reverse("api:accounts:login")
    response = client.post(
        url, {"email": email, "password": "StrongPass123!"}, format="json"
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_category(name="Buying Guides", slug="buying-guides"):
    """Create a blog category and return it."""
    return BlogCategory.objects.create(name=name, slug=slug, is_active=True)


def _make_post(slug="best-fridges-2026", **kwargs):
    """Create a published blog post defaulting to visible."""
    defaults = {
        "title": "Best fridges 2026",
        "body": "<p>Our guide to the best fridges.</p>",
        "excerpt": "A short guide.",
        "status": "published",
        "published_at": timezone.now() - datetime.timedelta(hours=1),
    }
    defaults.update(kwargs)
    return BlogPost.objects.create(slug=slug, **defaults)


def _make_product(name="Fridge 200L", slug="fridge-200l", sku="FRG-200-BLOG"):
    """Create a minimal product for related-product linking."""
    category, _ = Category.objects.get_or_create(
        name="Refrigerators", slug="refrigerators"
    )
    brand, _ = Brand.objects.get_or_create(name="Samsung", slug="samsung")
    counter = 0
    unique_slug, unique_sku = slug, sku
    while Product.objects.filter(slug=unique_slug).exists():
        counter += 1
        unique_slug = f"{slug}-{counter}"
    counter = 0
    while Product.objects.filter(sku=unique_sku).exists():
        counter += 1
        unique_sku = f"{sku}-{counter}"
    return Product.objects.create(
        name=name,
        slug=unique_slug,
        sku=unique_sku,
        description="A 200L refrigerator.",
        category=category,
        brand=brand,
        price="45000.00",
    )


class BlogPublicReadTests(APITestCase):
    """Exercise the anonymous storefront reads."""

    def test_list_returns_published_only(self):
        """Drafts never appear in the public list."""
        _make_post(slug="visible-post")
        _make_post(
            slug="draft-post",
            status="draft",
            published_at=None,
            title="Draft post",
        )
        url = reverse("api:content:blog-post-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slugs = [row["slug"] for row in response.data["results"]]
        self.assertIn("visible-post", slugs)
        self.assertNotIn("draft-post", slugs)

    def test_scheduled_post_hidden_until_due(self):
        """Future-dated posts return 404 on the detail view."""
        _make_post(
            slug="future-post",
            published_at=timezone.now() + datetime.timedelta(days=1),
        )
        url = reverse("api:content:blog-post-detail", args=["future-post"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_includes_seo_payload(self):
        """Detail carries meta fields, structured data, and related links."""
        category = _make_category()
        product = _make_product()
        post = _make_post(category=category, meta_title="Custom SEO title")
        post.related_products.add(product)
        url = reverse("api:content:blog-post-detail", args=[post.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["meta_title"], "Custom SEO title")
        self.assertEqual(response.data["json_ld"]["@type"], "Article")
        self.assertEqual(len(response.data["related_products"]), 1)
        self.assertEqual(response.data["related_products"][0]["slug"], product.slug)

    def test_list_query_count_stays_constant(self):
        """Listing posts with relations stays at a constant query count."""
        category = _make_category()
        for index in range(4):
            _make_post(slug=f"post-{index}", category=category)
        url = reverse("api:content:blog-post-list")
        with self.assertNumQueries(4):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_sitemap_and_feed_expose_published_posts(self):
        """Sitemap and RSS include published posts and skip drafts."""
        _make_post(slug="sitemap-post")
        _make_post(slug="sitemap-draft", status="draft", published_at=None)
        sitemap = self.client.get(reverse("api:content:blog-sitemap"))
        self.assertEqual(sitemap.status_code, status.HTTP_200_OK)
        self.assertIn("sitemap-post", sitemap.content.decode())
        self.assertNotIn("sitemap-draft", sitemap.content.decode())
        feed = self.client.get(reverse("api:content:blog-feed"))
        self.assertEqual(feed.status_code, status.HTTP_200_OK)
        self.assertIn("sitemap-post", feed.content.decode())

    def test_list_filters_by_linked_product(self):
        """The product filter powers related-guide strips on product pages."""
        product = _make_product()
        linked = _make_post(slug="linked-post")
        linked.related_products.add(product)
        _make_post(slug="unlinked-post")
        url = reverse("api:content:blog-post-list")
        response = self.client.get(url, {"product": product.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slugs = [row["slug"] for row in response.data["results"]]
        self.assertIn("linked-post", slugs)
        self.assertNotIn("unlinked-post", slugs)


class BlogAdminAccessTests(APITestCase):
    """Verify the admin endpoints reject anonymous and non-staff callers."""

    def test_anonymous_cannot_create_post(self):
        """Unauthenticated post creation is rejected."""
        url = reverse("api:content:blog-post-admin-create")
        response = self.client.post(
            url,
            {"title": "Nope", "slug": "nope", "body": "<p>x</p>"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_cannot_create_post(self):
        """A logged-in customer cannot create posts."""
        buyer = _make_user("buyer@example.com", "buyer")
        self.client.force_authenticate(user=buyer)
        url = reverse("api:content:blog-post-admin-create")
        response = self.client.post(
            url,
            {"title": "Nope", "slug": "nope", "body": "<p>x</p>"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_create_and_publish_post(self):
        """Staff create posts with tags and linked products."""
        _make_user("manager@example.com", "manager", is_staff=True)
        _login(self.client, "manager@example.com")
        tag = BlogTag.objects.create(name="Energy", slug="energy")
        product = _make_product()
        url = reverse("api:content:blog-post-admin-create")
        response = self.client.post(
            url,
            {
                "title": "Save power",
                "slug": "save-power",
                "body": "<p>Use efficient fridges.</p>",
                "status": "published",
                "published_at": timezone.now().isoformat(),
                "tag_ids": [tag.pk],
                "related_product_ids": [product.pk],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        post = BlogPost.objects.get(slug="save-power")
        self.assertEqual(post.tags.count(), 1)
        self.assertEqual(post.related_products.count(), 1)
        self.assertGreater(post.word_count, 0)


class BlogSanitisationTests(APITestCase):
    """Verify staff HTML is cleaned before storage."""

    def test_script_tags_stripped_on_create(self):
        """Embedded scripts never reach stored blog bodies."""
        post = create_blog_post(
            title="XSS test",
            slug="xss-test",
            body='<p>Hi</p><script>alert("x")</script>',
            status="draft",
        )
        self.assertNotIn("<script>", post.body)
        self.assertIn("Hi", post.body)


def _tiny_png(name="cover.png"):
    """Return a minimal valid PNG upload, rewound for reading."""
    buffer = BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = name
    return buffer


class BlogMultipartRelationTests(APITestCase):
    """Prove multipart writes carry tag/product lists alongside images."""

    def setUp(self):
        """Log in as staff for the admin endpoints."""
        _make_user("manager@example.com", "manager", is_staff=True)
        _login(self.client, "manager@example.com")

    def test_multipart_create_keeps_tags_products_and_cover(self):
        """Image + lists in one multipart body all persist together."""
        tag = BlogTag.objects.create(name="Energy", slug="energy")
        product = _make_product()
        url = reverse("api:content:blog-post-admin-create")
        response = self.client.post(
            url,
            {
                "title": "Save power",
                "slug": "save-power-multi",
                "body": "<p>Use efficient fridges.</p>",
                "status": "published",
                "published_at": timezone.now().isoformat(),
                "tag_ids": [tag.pk],
                "related_product_ids": [product.pk],
                "cover_image": _tiny_png(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        post = BlogPost.objects.get(slug="save-power-multi")
        self.assertTrue(bool(post.cover_image))
        self.assertEqual([row.pk for row in post.tags.all()], [tag.pk])
        self.assertEqual([row.pk for row in post.related_products.all()], [product.pk])

    def test_multipart_update_with_image_replaces_relations(self):
        """Attaching an image on edit updates relations instead of dropping them."""
        old_tag = BlogTag.objects.create(name="Old", slug="old")
        new_tag = BlogTag.objects.create(name="New", slug="new")
        product = _make_product()
        post = _make_post(slug="edit-multi")
        post.tags.add(old_tag)
        url = reverse("api:content:blog-post-admin-update", kwargs={"post_id": post.pk})
        response = self.client.patch(
            url,
            {
                "title": post.title,
                "cover_image": _tiny_png("new-cover.png"),
                "tag_ids": [new_tag.pk],
                "related_product_ids": [product.pk],
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        post.refresh_from_db()
        self.assertTrue(bool(post.cover_image))
        self.assertEqual([row.pk for row in post.tags.all()], [new_tag.pk])
        self.assertEqual([row.pk for row in post.related_products.all()], [product.pk])


class BlogSlugLockTests(APITestCase):
    """Published slugs are locked; drafts stay renameable."""

    def test_published_slug_change_rejected(self):
        """Renaming a post that stays published fails cleanly."""
        from django.core.exceptions import ValidationError

        post = _make_post(slug="locked-slug")
        with self.assertRaises(ValidationError):
            update_blog_post(post=post, slug="new-slug")
        post.refresh_from_db()
        self.assertEqual(post.slug, "locked-slug")

    def test_draft_slug_change_allowed(self):
        """Drafts rename freely since no live URL exists yet."""
        post = _make_post(
            slug="draft-slug", status="draft", published_at=None, title="Draft"
        )
        updated = update_blog_post(post=post, slug="renamed-draft")
        self.assertEqual(updated.slug, "renamed-draft")

    def test_unpublish_and_rename_together_allowed(self):
        """A post leaving the published state can rename in the same write."""
        post = _make_post(slug="going-dark")
        updated = update_blog_post(post=post, status="draft", slug="renamed-dark")
        self.assertEqual(updated.slug, "renamed-dark")
        self.assertEqual(updated.status, "draft")


class BlogFeedFilteringTests(APITestCase):
    """Crawler feeds never advertise hidden or unlive URLs."""

    def test_sitemap_and_feed_skip_noindex(self):
        """Noindex posts stay reachable but out of sitemap and RSS."""
        _make_post(slug="indexed-post")
        _make_post(slug="hidden-post", noindex=True)
        sitemap = self.client.get(reverse("api:content:blog-sitemap"))
        self.assertIn("indexed-post", sitemap.content.decode())
        self.assertNotIn("hidden-post", sitemap.content.decode())
        feed = self.client.get(reverse("api:content:blog-feed"))
        self.assertIn("indexed-post", feed.content.decode())
        self.assertNotIn("hidden-post", feed.content.decode())
        detail = self.client.get(
            reverse("api:content:blog-post-detail", args=["hidden-post"])
        )
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertTrue(detail.data["noindex"])

    def test_product_filter_excludes_scheduled_posts(self):
        """Related-guide lookups only return live posts, never drafts."""
        product = _make_product()
        live = _make_post(slug="live-guide")
        live.related_products.add(product)
        scheduled = _make_post(
            slug="scheduled-guide",
            published_at=timezone.now() + datetime.timedelta(days=1),
        )
        scheduled.related_products.add(product)
        url = reverse("api:content:blog-post-list")
        response = self.client.get(url, {"product": product.pk})
        slugs = [row["slug"] for row in response.data["results"]]
        self.assertIn("live-guide", slugs)
        self.assertNotIn("scheduled-guide", slugs)

    def test_page_size_caps_list_rows(self):
        """Small embeds can ask for just a few rows."""
        for index in range(4):
            _make_post(slug=f"cap-post-{index}")
        url = reverse("api:content:blog-post-list")
        response = self.client.get(url, {"page_size": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    def test_json_ld_carries_absolute_canonical_url(self):
        """Structured-data URLs are absolute and match the storefront path."""
        _make_post(slug="ld-post")
        url = reverse("api:content:blog-post-detail", args=["ld-post"])
        response = self.client.get(url)
        json_ld = response.data["json_ld"]
        self.assertTrue(json_ld["url"].startswith("http"))
        self.assertTrue(json_ld["url"].endswith("/blog/ld-post/"))
        self.assertEqual(json_ld["mainEntityOfPage"], json_ld["url"])


class BlogFeedPageTests(APITestCase):
    """Verify the retitled feed and its styled browser page.

    The feed and its stylesheet are public by design so browsers and feed
    readers can open them without credentials.
    """

    def test_feed_title_is_blogs(self):
        """The channel title reads Blogs and still lists published posts."""
        _make_post(slug="titled-post")
        response = self.client.get(reverse("api:content:blog-feed"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.content.decode()
        self.assertIn("<title>Blogs</title>", body)
        self.assertNotIn("<title>Blog</title>", body)
        self.assertIn("Practical appliance advice from our Nairobi team", body)
        self.assertIn("titled-post", body)

    def test_feed_links_browser_stylesheet(self):
        """The feed points browsers at the XSL page without breaking items."""
        _make_post(slug="styled-post")
        response = self.client.get(reverse("api:content:blog-feed"))
        body = response.content.decode()
        self.assertIn('<?xml-stylesheet type="text/xsl" href="feed.xsl"?>', body)
        self.assertIn("styled-post", body)

    def test_stylesheet_serves_blogs_page(self):
        """The stylesheet endpoint returns the Blogs listing transform."""
        url = reverse("api:content:blog-feed-stylesheet")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("text/xsl", response["Content-Type"])
        body = response.content.decode()
        self.assertIn("<h1>Buying guides &amp; tips</h1>", body)
        self.assertIn('<p class="lede">', body)
