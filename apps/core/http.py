"""Shared HTTP helpers for request-time trust decisions.

The SMS delivery-report callback derives the caller's source IP to check it
against a provider allowlist. That derivation must agree
with the identity the throttles use, or an attacker could rotate fake
``X-Forwarded-For`` values past the rate limiter while the IP check trusts a
different value. The single implementation below mirrors DRF's ``get_ident``
so both features always agree.
"""

from rest_framework.settings import api_settings


def client_ip(request):
    """Return the trustable client IP for a request.

    When ``NUM_PROXIES`` is configured the header chain is walked from the
    right: the last ``NUM_PROXIES`` entries are the trusted reverse proxy hops,
    and the entry immediately before them is the actual client as first seen
    by the trusted proxy. This makes the value consistent with DRF's rate
    limiting, which uses the same rule, so spoofed header chains cannot make
    the IP allowlist and the throttle disagree.

    Args:
        request: the incoming HTTP request.

    Returns:
        str: the client IP address.
    """
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


def is_client_ip_allowed(request, allowlist):
    """Return whether a request's client IP is in an allowlist.

    An empty allowlist passes every request so local development without a
    configured provider IP range keeps working; deployments opt into the check
    by setting the list.

    Args:
        request: the incoming HTTP request.
        allowlist (list[str]): the trusted source IPs.

    Returns:
        bool: True when the allowlist is empty or the client IP is listed.
    """
    if not allowlist:
        return True
    return client_ip(request) in allowlist
