"""Tests for client IP resolution and throttle identity behind proxies.

The rate limiter and the callback IP allowlist both depend on a single resolved
client identity. These tests pin the Cloudflare trust rule and prove that
distinct visitors no longer share a throttle bucket when requests arrive from a
shared Cloudflare edge address.
"""

from django.conf import settings
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.http import client_ip

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
