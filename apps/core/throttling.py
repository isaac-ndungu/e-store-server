"""Rate-limiting classes that identify clients behind the deployment proxies.

DRF's stock throttles key anonymous callers on ``REMOTE_ADDR``. Behind
Cloudflare that address is a shared edge IP, so every visitor lands in the same
bucket and a normal listing page exhausts the budget for everyone. The throttle
below delegates identity resolution to ``apps.core.http.client_ip`` so throttling
and the callback IP allowlist agree, including the verified Cloudflare
``CF-Connecting-IP`` path.
"""

from rest_framework.throttling import ScopedRateThrottle as _ScopedRateThrottle

from apps.core.http import client_ip


class ClientIPScopedRateThrottle(_ScopedRateThrottle):
    """Scoped throttle that keys anonymous callers on the resolved client IP."""

    def get_ident(self, request):
        """Return the resolved client identity for this request.

        Args:
            request: the incoming HTTP request.

        Returns:
            str: the client IP, resolved through the trusted proxy chain.
        """
        return client_ip(request)
