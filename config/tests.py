"""Tests for the health endpoint.

A smoke test so CI (and Postman) can confirm the backend serves requests.
"""

from django.test import SimpleTestCase
from django.urls import reverse


class HealthEndpointTests(SimpleTestCase):
    """Verify the liveness probe responds correctly."""

    def test_health_returns_ok(self):
        """The health endpoint reports the backend is up and reachable."""
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_api_v1_mount_is_accessible(self):
        """The /api/v1/ mount exists and responds, fail-closed by default.

        The DRF API root is authenticated by default (global default permission
        is ``IsAuthenticated``), so an anonymous caller receives 401 rather than
        404  -  proving the mount resolves without exposing anything publicly.
        """
        response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 401)


class RobotsEndpointTests(SimpleTestCase):
    """Verify crawlers are kept off the API surface."""

    def test_robots_disallows_all_agents(self):
        """Anonymous callers get a plain-text policy disallowing everything."""
        response = self.client.get(reverse("robots"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response["Content-Type"].startswith("text/plain"),
        )
        body = response.content.decode()
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)


class SecurityHeadersTests(SimpleTestCase):
    """Verify every response carries the baseline hardening headers."""

    def test_health_carries_hardening_headers(self):
        """The public probe returns CSP, Permissions-Policy, and COOP."""
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertIn("Permissions-Policy", response.headers)
        self.assertIn("Cross-Origin-Opener-Policy", response.headers)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn(
            "frame-ancestors 'none'", response.headers["Content-Security-Policy"]
        )

    def test_middleware_never_overrides_view_headers(self):
        """An explicit view-level COOP value survives the middleware."""
        response = self.client.get("/api/swagger/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cross-Origin-Opener-Policy", response.headers)

    def test_docs_pages_carry_private_cache_control(self):
        """Docs pages allow private browser caching; other pages set none."""
        for path in ("/api/swagger/", "/api/redoc/", "/api/schema/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers.get("Cache-Control"), "private, max-age=3600"
            )
        response = self.client.get(reverse("health"))
        self.assertIsNone(response.headers.get("Cache-Control"))


class DocsPagesTests(SimpleTestCase):
    """Verify the API docs pages declare language, description, and heading."""

    def test_swagger_page_has_lang_description_and_heading(self):
        """Swagger UI exposes lang, meta description, and an h1 for AT."""
        response = self.client.get("/api/swagger/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('<html lang="en">', content)
        self.assertIn('name="description"', content)
        self.assertIn("<h1", content)

    def test_redoc_page_has_lang_description_and_heading(self):
        """Redoc exposes lang, meta description, and an h1 for AT."""
        response = self.client.get("/api/redoc/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('<html lang="en">', content)
        self.assertIn('name="description"', content)
        self.assertIn("<h1", content)

    def test_docs_pages_have_canonical_and_landmarks(self):
        """Both docs pages carry a canonical link, a main landmark, and nav."""
        for path in ("/api/swagger/", "/api/redoc/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            content = response.content.decode()
            self.assertIn('rel="canonical"', content)
            self.assertIn("<main", content)
            self.assertIn("<nav", content)
            self.assertIn('href="#docs-main"', content)
            self.assertIn('href="/api/redoc/"', content)
            self.assertIn('href="/api/swagger/"', content)
            self.assertIn('href="/api/schema/"', content)

    def test_swagger_cdn_assets_are_pinned_with_integrity(self):
        """Swagger CDN scripts and stylesheets pin versions and SRI hashes."""
        content = self.client.get("/api/swagger/").content.decode()
        self.assertIn("@5.33.1", content)
        self.assertNotIn("@latest", content)
        self.assertIn('integrity="sha384-', content)
        self.assertIn('crossorigin="anonymous"', content)

    def test_redoc_cdn_asset_is_pinned_with_integrity(self):
        """The Redoc script pins its version and carries an SRI hash."""
        content = self.client.get("/api/redoc/").content.decode()
        self.assertIn("@2.5.4", content)
        self.assertNotIn("@latest", content)
        self.assertIn('integrity="sha384-', content)
        self.assertIn('crossorigin="anonymous"', content)

    def test_redoc_has_favicon_and_no_third_party_fonts(self):
        """Redoc declares a favicon and loads no hash-less font stylesheets."""
        content = self.client.get("/api/redoc/").content.decode()
        self.assertIn('rel="icon"', content)
        self.assertNotIn("fonts.googleapis.com", content)
        self.assertNotIn("fonts.gstatic.com", content)

    def test_docs_pages_are_gzip_compressed(self):
        """A gzip-capable client receives compressed docs HTML."""
        response = self.client.get("/api/swagger/", HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
