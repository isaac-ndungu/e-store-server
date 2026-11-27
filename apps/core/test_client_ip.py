"""Tests for client IP resolution and throttle identity behind proxies.

The rate limiter and the callback IP allowlist both depend on a single resolved
client identity. These tests pin the Cloudflare trust rule and prove that
distinct visitors no longer share a throttle bucket when requests arrive from a
shared Cloudflare edge address.
"""

from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.http import client_ip
from apps.core.throttling import ClientIPScopedRateThrottle

CLOUDFLARE_EDGE = "173.245.48.10"
NON_CLOUDFLARE_PEER = "198.51.100.7"
LOGIN_URL = reverse("api:accounts:login")
LOGIN_PAYLOAD = {"email": "nobody@example.com", "password": "WrongPass123!"}


def _request(**meta):
    """Build a GET request with the supplied WSGI metadata.

    Args:
        **meta: WSGI ``META`` entries such as ``REMOTE_ADDR``.

    Returns:
        HttpRequest: the built request.
    """
    return RequestFactory().get("/", **meta)


class ClientIPResolutionTests(SimpleTestCase):
    """Exercises the resolved identity without touching the throttle cache."""

    def test_cloudflare_header_is_ignored_when_disabled(self):
        """The default configuration never trusts CF-Connecting-IP."""
        request = _request(
            REMOTE_ADDR=CLOUDFLARE_EDGE,
            HTTP_CF_CONNECTING_IP="203.0.113.10",
        )
        self.assertEqual(client_ip(request), CLOUDFLARE_EDGE)

    @override_settings(TRUST_CLOUDFLARE_HEADERS=True)
    def test_cloudflare_header_is_used_from_a_cloudflare_peer(self):
        """A verified Cloudflare peer's CF-Connecting-IP becomes the identity."""
        request = _request(
            REMOTE_ADDR=CLOUDFLARE_EDGE,
            HTTP_CF_CONNECTING_IP="203.0.113.10",
        )
        self.assertEqual(client_ip(request), "203.0.113.10")

    @override_settings(TRUST_CLOUDFLARE_HEADERS=True)
    def test_cloudflare_header_is_ignored_from_a_non_cloudflare_peer(self):
        """A direct caller cannot forge the header to escape its own bucket."""
        request = _request(
            REMOTE_ADDR=NON_CLOUDFLARE_PEER,
            HTTP_CF_CONNECTING_IP="203.0.113.10",
        )
        self.assertEqual(client_ip(request), NON_CLOUDFLARE_PEER)

    @override_settings(TRUST_CLOUDFLARE_HEADERS=True)
    def test_invalid_cloudflare_header_falls_back_to_the_peer(self):
        """A malformed or comma-joined header value is not trusted."""
        for value in ("not-an-ip", "203.0.113.10, 203.0.113.11", ""):
            with self.subTest(value=value):
                request = _request(
                    REMOTE_ADDR=CLOUDFLARE_EDGE,
                    HTTP_CF_CONNECTING_IP=value,
                )
                self.assertEqual(client_ip(request), CLOUDFLARE_EDGE)

    @override_settings(TRUST_CLOUDFLARE_HEADERS=True)
    def test_configured_cloudflare_ranges_override_the_bundled_list(self):
        """Custom ranges let operators track a provider edge change."""
        request = _request(
            REMOTE_ADDR="192.0.2.20",
            HTTP_CF_CONNECTING_IP="203.0.113.10",
        )
        with self.settings(CLOUDFLARE_TRUSTED_IP_RANGES=("192.0.2.0/24",)):
            self.assertEqual(client_ip(request), "203.0.113.10")

    def test_proxy_chain_resolution_still_honours_num_proxies(self):
        """Without Cloudflare, the standard right-to-left chain walk is intact."""
        request = _request(
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.10, 198.51.100.1",
        )
        rest_framework_settings = {
            **settings.REST_FRAMEWORK,
            "NUM_PROXIES": 1,
        }
        with self.settings(REST_FRAMEWORK=rest_framework_settings):
            self.assertEqual(client_ip(request), "198.51.100.1")


@override_settings(TRUST_CLOUDFLARE_HEADERS=True)
class CloudflareThrottleIdentityTests(APITestCase):
    """Proves the throttle keys anonymous callers on the verified client IP.

    The login endpoint uses the strict ``auth_login`` scope (three requests per
    minute), so exhausting a bucket takes four requests rather than hundreds.
    """

    def setUp(self):
        cache.clear()

    def _login(self, client_ip_address, *, remote_addr=CLOUDFLARE_EDGE):
        """Post a failed login carrying the supplied client identity.

        Args:
            client_ip_address (str): the asserted CF-Connecting-IP value.
            remote_addr (str): the immediate peer address.

        Returns:
            Response: the login response.
        """
        return self.client.post(
            LOGIN_URL,
            LOGIN_PAYLOAD,
            format="json",
            REMOTE_ADDR=remote_addr,
            HTTP_CF_CONNECTING_IP=client_ip_address,
        )

    def test_distinct_visitors_behind_one_edge_get_separate_buckets(self):
        """Two visitors sharing a Cloudflare edge do not share a counter."""
        for _ in range(3):
            self.assertEqual(
                self._login("203.0.113.10").status_code,
                status.HTTP_401_UNAUTHORIZED,
            )
        self.assertEqual(
            self._login("203.0.113.10").status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
        self.assertEqual(
            self._login("203.0.113.11").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_a_spoofed_header_from_a_non_cloudflare_peer_shares_one_bucket(self):
        """Rotating the header without a Cloudflare peer does not reset the count."""
        for index in range(3):
            response = self._login(
                f"203.0.113.{index}",
                remote_addr=NON_CLOUDFLARE_PEER,
            )
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self._login("203.0.113.99", remote_addr=NON_CLOUDFLARE_PEER)
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


@override_settings(INTERNAL_API_TOKEN="test-internal-token")
class InternalRenderThrottleTests(SimpleTestCase):
    """Checks the authenticated internal scope and protected public budgets."""

    def setUp(self):
        cache.clear()

    def _throttle(self, scope, **headers):
        """Return a throttle and its request for one view scope."""
        request = RequestFactory().get(
            "/api/v1/products/",
            REMOTE_ADDR="198.51.100.40",
            **headers,
        )
        request.user = AnonymousUser()
        throttle = ClientIPScopedRateThrottle()
        view = SimpleNamespace(throttle_scope=scope)
        self.assertTrue(throttle.allow_request(request, view))
        return throttle, request, view

    def test_valid_server_token_uses_internal_scope_and_forwarded_ip(self):
        """Trusted server catalog reads use their own scope and visitor key."""
        throttle, request, view = self._throttle(
            "public_catalog",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
            HTTP_X_FORWARDED_FOR="203.0.113.25, 198.51.100.2",
        )
        self.assertEqual(throttle.get_rate(), "3000/min")
        self.assertEqual(
            throttle.get_cache_key(request, view),
            "throttle_internal_render_203.0.113.25",
        )

    def test_invalid_missing_and_browser_tokens_keep_public_scope(self):
        """Untrusted or browser-supplied tokens do not change the rate bucket."""
        cases = (
            {},
            {"HTTP_X_INTERNAL_TOKEN": "wrong-token"},
            {
                "HTTP_X_INTERNAL_TOKEN": "test-internal-token",
                "HTTP_ORIGIN": "https://shop.example.com",
            },
            {
                "HTTP_X_INTERNAL_TOKEN": "test-internal-token",
                "HTTP_SEC_FETCH_SITE": "same-origin",
            },
        )
        for headers in cases:
            with self.subTest(headers=headers):
                throttle, request, view = self._throttle("public_catalog", **headers)
                self.assertEqual(throttle.get_rate(), "100/min")
                self.assertEqual(
                    throttle.get_cache_key(request, view),
                    "throttle_public_catalog_198.51.100.40",
                )

    def test_sensitive_scope_stays_strict_with_valid_server_token(self):
        """An internal header never raises auth, inquiry, coupon, or SMS limits."""
        for scope, rate in (
            ("auth_login", "3/min"),
            ("password_reset_ip", "5/min"),
            ("coupon_validate", "5/min"),
            ("inquiry_write", "20/min"),
            ("notification_send", "5/min"),
        ):
            with self.subTest(scope=scope):
                throttle, request, view = self._throttle(
                    scope,
                    HTTP_X_INTERNAL_TOKEN="test-internal-token",
                )
                self.assertEqual(throttle.get_rate(), rate)
                self.assertEqual(
                    throttle.get_cache_key(request, view),
                    f"throttle_{scope}_198.51.100.40",
                )

    def test_post_with_valid_server_token_keeps_public_scope(self):
        """The internal scope is limited to safe reads, never writes."""
        request = RequestFactory().post(
            "/api/v1/products/",
            REMOTE_ADDR="198.51.100.40",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )
        request.user = AnonymousUser()
        throttle = ClientIPScopedRateThrottle()
        view = SimpleNamespace(throttle_scope="public_catalog")
        self.assertTrue(throttle.allow_request(request, view))
        self.assertEqual(throttle.get_rate(), "100/min")

    def test_whitelisted_public_read_uses_internal_scope(self):
        """Bundle and collection reads are catalogue data served at the high limit."""
        for path in (
            "/api/v1/bundles/",
            "/api/v1/collections/",
            "/api/v1/social-proof/viewers/",
        ):
            with self.subTest(path=path):
                request = RequestFactory().get(
                    path,
                    REMOTE_ADDR="198.51.100.40",
                    HTTP_X_INTERNAL_TOKEN="test-internal-token",
                )
                request.user = AnonymousUser()
                throttle = ClientIPScopedRateThrottle()
                view = SimpleNamespace(throttle_scope="public")
                self.assertTrue(throttle.allow_request(request, view))
                self.assertEqual(throttle.get_rate(), "3000/min")

    def test_public_site_config_does_not_use_internal_scope(self):
        """The generic public scope stays unchanged outside catalog routes."""
        request = RequestFactory().get(
            "/api/v1/site-config/",
            REMOTE_ADDR="198.51.100.40",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )
        request.user = AnonymousUser()
        throttle = ClientIPScopedRateThrottle()
        view = SimpleNamespace(throttle_scope="public")
        self.assertTrue(throttle.allow_request(request, view))
        self.assertEqual(throttle.get_rate(), "100/min")
