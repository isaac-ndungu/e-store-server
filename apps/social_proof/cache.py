"""Redis-backed live-viewer counters for the social proof app.

The storefront urgency signal ("N people are viewing this right now") is
served entirely from Redis so the read is one cheap request and never touches
the view-event table. Each product keeps a set of currently-active session
keys with a rolling TTL window: adding a key re-arms the window, so a viewer
stays counted while they keep browsing and drops off on their own once idle.
``SADD``/``SCARD`` keep the increment and the read atomic under concurrent
traffic.

When the configured cache backend is not Redis (the test settings swap in
LocMem), the same contract is emulated with an in-memory mapping of session
keys to last-seen timestamps, guarded by a lock so concurrent writes do not
lose updates. The window is a bounded constant; no background sweep is needed
because each key expires with the set.
"""

import threading
import time

from django.core.cache import cache

LIVE_VIEWER_WINDOW_SECONDS = 5 * 60

_locmem_lock = threading.Lock()


def _live_key(product_id):
    """Return the storage key holding a product's live-viewer set.

    Args:
        product_id (int): the product primary key.

    Returns:
        str: the cache key.
    """
    return f"product:live_viewers:{product_id}"


def _backend_is_redis():
    """Return whether the configured cache backend is Redis.

    Returns:
        bool: True when the default cache is a RedisCache instance.
    """
    return "RedisCache" in type(cache).__name__


def _add_live_viewer_locmem(product_id, session_key):
    """Record a viewer against a product using the non-Redis cache.

    Stores a mapping of session keys to last-seen epoch seconds in one cache
    value per product; entries older than the window are pruned on write so the
    read counts exactly what the Redis set would. Wrapped in a lock because the
    read-modify-write is not atomic on the non-Redis backend.

    Args:
        product_id (int): the product primary key.
        session_key (str): the browsing session identifier.
    """
    key = _live_key(product_id)
    cutoff = time.time() - LIVE_VIEWER_WINDOW_SECONDS
    with _locmem_lock:
        key_and_ts = cache.get(key) or {}
        key_and_ts = {s: ts for s, ts in key_and_ts.items() if ts >= cutoff}
        key_and_ts[session_key] = time.time()
        cache.set(key, key_and_ts, LIVE_VIEWER_WINDOW_SECONDS)


def add_live_viewer(product_id, session_key):
    """Add a session to a product's live-viewer set.

    Re-arms the TTL window so an active viewer stays counted. Idempotent per
    session: the same session added repeatedly within the window still counts
    once.

    Args:
        product_id (int): the product primary key.
        session_key (str): the browsing session identifier.
    """
    if _backend_is_redis():
        client = cache.client.get_client()
        with client.pipeline() as pipe:
            pipe.sadd(_live_key(product_id), session_key)
            pipe.expire(_live_key(product_id), LIVE_VIEWER_WINDOW_SECONDS)
            pipe.execute()
        return
    _add_live_viewer_locmem(product_id, session_key)


def get_live_viewer_count(product_id):
    """Return the number of sessions currently viewing a product.

    Args:
        product_id (int): the product primary key.

    Returns:
        int: the distinct live-viewer count.
    """
    if _backend_is_redis():
        client = cache.client.get_client()
        return client.scard(_live_key(product_id))
    key_and_ts = cache.get(_live_key(product_id)) or {}
    cutoff = time.time() - LIVE_VIEWER_WINDOW_SECONDS
    return sum(1 for ts in key_and_ts.values() if ts >= cutoff)
