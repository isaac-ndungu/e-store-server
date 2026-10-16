"""Crawler feeds for the blog: XML sitemap and RSS.

Both feeds expose only published posts so drafts never leak to crawlers.
Absolute post URLs are built from the storefront base URL, since this API
never serves HTML itself  -  the storefront owns the crawlable pages and
points its sitemap index at these endpoints.
"""

from django.conf import settings
from django.contrib.syndication.views import Feed
from django.http import HttpResponse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.content.models import BlogPost
from apps.content.selectors import list_active_blog_categories, list_published_posts


def _storefront_base():
    """Return the storefront base URL without a trailing slash.

    Returns:
        str: the configured frontend URL.
    """
    return settings.FRONTEND_URL.rstrip("/")


def _feed_posts():
    """Return published posts eligible for crawler feeds.

    Drafts, scheduled posts, and staff-flagged noindex posts are all
    excluded so feeds never advertise a URL crawlers should not index.

    Returns:
        QuerySet: feed-eligible posts, newest first.
    """
    return list_published_posts(exclude_noindex=True)


class BlogSitemapView(View):
    """Render an XML sitemap of published posts and active categories."""

    def get(self, request):
        """Return the sitemap XML.

        Args:
            request: the GET request (unused beyond the handler signature).

        Returns:
            HttpResponse: the sitemap document.
        """
        base = _storefront_base()
        categories = list_active_blog_categories()
        posts = (
            BlogPost.objects.filter(
                status="published",
                published_at__lte=timezone.now(),
                noindex=False,
            )
            .only("slug", "updated_at")
            .order_by("-published_at", "-pk")
        )
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for category in categories:
            lines.append(
                f"<url><loc>{base}/blog?category={category.slug}</loc>"
                f"<changefreq>monthly</changefreq>"
                f"<priority>0.5</priority></url>"
            )
        for post in posts.only("slug", "updated_at"):
            lastmod = post.updated_at.date().isoformat()
            lines.append(
                f"<url><loc>{base}/blog/{post.slug}/</loc>"
                f"<lastmod>{lastmod}</lastmod>"
                f"<changefreq>weekly</changefreq>"
                f"<priority>0.7</priority></url>"
            )
        lines.append("</urlset>")
        return HttpResponse("\n".join(lines), content_type="application/xml")


class BlogFeed(Feed):
    """RSS feed of the latest published blog posts.

    The response carries an ``xml-stylesheet`` processing instruction so a
    browser opening the feed URL renders a readable "Blogs" page instead of
    a raw XML tree. Feed readers ignore the instruction and parse the RSS
    items unchanged.
    """

    title = "Blogs"
    link = "/blog/"
    description = (
        "Practical appliance advice from our Nairobi team: buying guides, "
        "energy-saving tips, and care instructions."
    )
    stylesheet_href = "feed.xsl"

    def __call__(self, request, *args, **kwargs):
        """Return the RSS document with the browser stylesheet attached.

        Args:
            request: the GET request.
            *args: positional feed arguments.
            **kwargs: keyword feed arguments.

        Returns:
            HttpResponse: the RSS document with a stylesheet instruction
                inserted after the XML declaration.
        """
        response = super().__call__(request, *args, **kwargs)
        try:
            document = response.content.decode("utf-8")
        except UnicodeDecodeError:
            return response
        instruction = (
            f'<?xml-stylesheet type="text/xsl" href="{self.stylesheet_href}"?>'
        )
        if document.startswith("<?xml") and instruction not in document:
            end = document.index("?>") + 2
            document = document[:end] + "\n" + instruction + document[end:]
            response.content = document.encode("utf-8")
        return response

    def items(self):
        """Return the latest published posts.

        Returns:
            QuerySet: up to 20 published posts, newest first.
        """
        return list_published_posts(exclude_noindex=True)[:20]

    def item_title(self, item):
        """Return the feed title for a post.

        Args:
            item (BlogPost): the post.

        Returns:
            str: the post title.
        """
        return item.title

    def item_description(self, item):
        """Return the feed summary for a post.

        Args:
            item (BlogPost): the post.

        Returns:
            str: the excerpt, falling back to the meta description.
        """
        return item.excerpt or item.meta_description

    def item_link(self, item):
        """Return the storefront URL for a post.

        Args:
            item (BlogPost): the post.

        Returns:
            str: the absolute storefront URL.
        """
        return f"{_storefront_base()}/blog/{item.slug}/"

    def item_pubdate(self, item):
        """Return the publish date for a post.

        Args:
            item (BlogPost): the post.

        Returns:
            datetime: the publish timestamp or creation time.
        """
        return item.published_at or item.created_at or timezone.now()


class BlogFeedStylesheetView(TemplateView):
    """Serve the XSLT stylesheet behind the styled feed page.

    The transform turns the RSS document into a responsive, high-contrast
    "Blogs" listing (header, article cards, footer) for browsers that open
    the feed URL directly. It makes no network requests of its own so the
    page stays light on slow connections.
    """

    template_name = "content/blog_feed.xsl"
    content_type = "text/xsl; charset=utf-8"
