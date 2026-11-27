"""Rate-limiting classes that identify callers behind deployment proxies.

DRF's stock throttles key anonymous callers on ``REMOTE_ADDR``. Behind
Cloudflare that address is a shared edge IP, so every visitor lands in the same
bucket and a normal listing page exhausts the budget for everyone. The throttle
below delegates identity resolution to ``apps.core.http.client_ip`` so throttling
and the callback IP allowlist agree, including the verified Cloudflare
``CF-Connecting-IP`` path. Authenticated storefront server reads use a separate
scope without relaxing limits on writes or sensitive public endpoints.
"""

import hmac
import ipaddress

from django.conf import settings
from rest_framework.throttling import ScopedRateThrottle as _ScopedRateThrottle

from apps.core.http import client_ip

_INTERNAL_CATALOG_SCOPES = frozenset(
    {"catalog_suggest", "product_price", "public_catalog"}
)
_INTERNAL_PUBLIC_PATHS = (
    "/api/v1/bundles/",
    "/api/v1/collections/",
    "/api/v1/social-proof/viewers/",
)


class ClientIPScopedRateThrottle(_ScopedRateThrottle):
    """Apply per-client scopes and a separate limit to trusted storefront reads."""

    def allow_request(self, request, view):
        """Select the internal scope only for trusted, read-only catalog requests.

        Args:
            request: the incoming HTTP request.
            view: the view being throttled.

        Returns:
            bool: whether the request is within its configured rate limit.
        """
        self._internal_render = self._is_internal_catalog_request(request, view)
        return super().allow_request(request, view)

    def get_rate(self):
        """Return the internal render rate when the request qualifies."""
        if getattr(self, "_internal_render", False):
            return self.THROTTLE_RATES.get("internal_render")
        return super().get_rate()

    def get_cache_key(self, request, view):
        """Return a cache key isolated from browser-facing throttle scopes."""
        if getattr(self, "_internal_render", False):
            return self.cache_format % {
                "scope": "internal_render",
                "ident": self.get_ident(request),
            }
        return super().get_cache_key(request, view)

    def get_ident(self, request):
        """Return the resolved client identity for this request.

        Args:
            request: the incoming HTTP request.

        Returns:
            str: the client IP, resolved through the trusted proxy chain.
        """
        if getattr(self, "_internal_render", False):
            return self._internal_client_ident(request)
        return client_ip(request)

    @classmethod
    def _is_internal_catalog_request(cls, request, view):
        """Validate the shared token and restrict its use to safe catalog reads."""
        if request.method != "GET":
            return False
        if request.headers.get("Origin") is not None:
            return False
        if request.headers.get("Sec-Fetch-Site") is not None:
            return False

        expected = getattr(settings, "INTERNAL_API_TOKEN", "")
        supplied = request.headers.get("X-Internal-Token", "")
        if not expected or not supplied:
            return False
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return False

        scope = getattr(view, "throttle_scope", None)
        if scope in _INTERNAL_CATALOG_SCOPES:
            return True
        if scope == "public":
            return request.path_info.startswith(_INTERNAL_PUBLIC_PATHS)
        return False

    @staticmethod
    def _internal_client_ident(request):
        """Use a valid forwarded visitor IP or a shared storefront identity."""
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        candidate = forwarded_for.split(",", 1)[0].strip()
        if candidate:
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return "storefront"
