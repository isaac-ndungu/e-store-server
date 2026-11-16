"""Shared HTTP helpers for request-time trust decisions.

Two features derive the caller's source IP: the SMS delivery-report callback
checks it against a provider allowlist, and DRF's rate limiter uses it to
namespace per-client counters. Both must agree, or a caller could rotate fake
``X-Forwarded-For`` values past the throttle while the IP check trusts a
different value.

When the origin sits behind Cloudflare, every request reaches Django from a
Cloudflare edge address, so the raw peer address is shared by all visitors.
Cloudflare supplies the real client address in ``CF-Connecting-IP``. That
header is only trusted when the request's peer address is inside Cloudflare's
published edge ranges and ``TRUST_CLOUDFLARE_HEADERS`` is enabled, so a direct
request cannot forge it.

The header chain used is::

    client -> Cloudflare edge -> OpenLiteSpeed -> Django

    CF-Connecting-IP: rewritten by the Cloudflare edge to the client address
    X-Forwarded-For:  appended by Cloudflare and again by OpenLiteSpeed
    REMOTE_ADDR:      the peer that connected to Django (the Cloudflare edge
                      when OpenLiteSpeed passes the socket peer through)

When the Cloudflare header is not trusted, the chain is resolved the same way
DRF does: with ``NUM_PROXIES`` set, walk ``X-Forwarded-For`` from the right and
take the entry immediately before the trusted proxy hops.
"""

import ipaddress
from functools import lru_cache

from django.conf import settings
from rest_framework.settings import api_settings

# Published Cloudflare edge ranges (https://www.cloudflare.com/ips/). Deployments
# can override these with the CLOUDFLARE_TRUSTED_IP_RANGES setting.
_DEFAULT_CLOUDFLARE_RANGES = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_CLOUDFLARE_CLIENT_HEADER = "HTTP_CF_CONNECTING_IP"


@lru_cache(maxsize=8)
def _parse_networks(cidrs):
    """Return parsed networks for a tuple of CIDR strings.

    Args:
        cidrs (tuple[str, ...]): the CIDR strings to parse.

    Returns:
        tuple: the parsed ``ipaddress`` networks, skipping invalid entries.
    """
    networks = []
    for value in cidrs:
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _cloudflare_networks():
    """Return the Cloudflare edge ranges to trust for this deployment.

    Returns:
        tuple: the parsed networks, from the setting when provided or the
            bundled published ranges otherwise.
    """
    configured = tuple(
        value
        for value in getattr(settings, "CLOUDFLARE_TRUSTED_IP_RANGES", ())
        if value
    )
    return _parse_networks(configured or _DEFAULT_CLOUDFLARE_RANGES)


def _is_cloudflare_address(address):
    """Return whether an address belongs to a trusted Cloudflare edge range.

    Args:
        address (str): the IP address to test.

    Returns:
        bool: True when the address is inside a configured Cloudflare range.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(
        parsed.version == network.version and parsed in network
        for network in _cloudflare_networks()
    )


def _cloudflare_client_ip(request):
    """Return the client IP asserted by a trusted Cloudflare edge, if any.

    Trust requires all three conditions: the feature is enabled, the immediate
    peer is a Cloudflare edge address, and ``CF-Connecting-IP`` holds a single
    valid IP address. Any failure returns ``None`` so the caller falls back to
    the standard proxy-chain resolution.

    Args:
        request: the incoming HTTP request.

    Returns:
        str | None: the asserted client IP, or None when it cannot be trusted.
    """
    if not getattr(settings, "TRUST_CLOUDFLARE_HEADERS", False):
        return None

    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()
    if not _is_cloudflare_address(remote_addr):
        return None

    header_ip = (request.META.get(_CLOUDFLARE_CLIENT_HEADER) or "").strip()
    if not header_ip:
        return None
    try:
        ipaddress.ip_address(header_ip)
    except ValueError:
        return None
    return header_ip


def client_ip(request):
    """Return the trustable client IP for a request.

    A trusted Cloudflare edge's ``CF-Connecting-IP`` wins when enabled and the
    peer address verifies as Cloudflare. Otherwise ``NUM_PROXIES`` is honoured:
    the last ``NUM_PROXIES`` entries of ``X-Forwarded-For`` are the trusted
    reverse proxy hops, and the entry immediately before them is the actual
    client as first seen by the trusted proxy.

    Args:
        request: the incoming HTTP request.

    Returns:
        str: the client IP address.
    """
    cloudflare_ip = _cloudflare_client_ip(request)
    if cloudflare_ip is not None:
        return cloudflare_ip

    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    remote_addr = request.META.get("REMOTE_ADDR", "")
    num_proxies = api_settings.NUM_PROXIES

    if num_proxies is not None:
        if num_proxies == 0 or xff is None:
            return remote_addr
        addrs = xff.split(",")
        proxied = addrs[-min(num_proxies, len(addrs))]
        return proxied.strip()

    return "".join(xff.split()) if xff else remote_addr
