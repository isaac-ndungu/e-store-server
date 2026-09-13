"""Shared idempotency-key handling for mutating endpoints.

A client that repeats a request (a retried tap, a network-level redelivery)
must not re-run the mutation. The caller supplies an ``Idempotency-Key``
header; the first successful response is stored under that key and replayed
verbatim on later requests with the same key, while a request already being
processed under the same key is answered with 409 rather than being run twice.

A key is bound to the request it first succeeded with: repeating the key
with a *different* body is answered with 409 instead of replaying the first
result, so a stale or recycled UUID cannot silently return the wrong order,
ticket, or refund. Keys live 24 hours; only successful responses are cached —
a failed (validation) attempt can be retried with the same key after the
caller fixes the payload.
"""

import hashlib

from django.core.cache import cache
from rest_framework import status as http_status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
PROCESSING_LOCK_TTL_SECONDS = 60
IDEMPOTENT_STATUS_CODES = {http_status.HTTP_201_CREATED, http_status.HTTP_200_OK}


def require_idempotency_key(request):
    """Return the required idempotency key for a request.

    Args:
        request: the incoming request.

    Returns:
        str: the idempotency key value.

    Raises:
        ValidationError: if the header is missing or empty.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not key:
        raise ValidationError(
            {
                IDEMPOTENCY_HEADER: "This request must be sent with an Idempotency-Key header."
            }
        )
    return key


def _cache_key(scope, key):
    """Return the storage key for a caller scope + idempotency key pair.

    The scope is any stable string identifying the acting caller — an
    authenticated user is ``u<id>`` and an anonymous guest is ``s<session>``.
    Two guests must never share a scope, or one guest's stored response
    (order detail, lookup token) could be replayed to another.

    Args:
        scope (str | int): the acting caller's scope.
        key (str): the idempotency key.

    Returns:
        str: the cache key.
    """
    return f"idempotency:{scope}:{key}"


def read_cached_result(scope, key):
    """Return a previously stored response, if any.

    Args:
        scope (str | int): the acting caller's scope.
        key (str): the idempotency key.

    Returns:
        dict | None: ``{"status": int, "data": dict}`` for a prior success,
            else ``None``.
    """
    return cache.get(_cache_key(scope, key))


def store_result(scope, key, status_code, data, payload_hash=None):
    """Persist a successful response under an idempotency key.

    Args:
        scope (str | int): the acting caller's scope.
        key (str): the idempotency key.
        status_code (int): the HTTP status to replay.
        data: the response payload to replay.
        payload_hash (str | None): fingerprint of the request that produced
            the result, bound to the key for mismatch detection.

    Returns:
        None
    """
    cache.set(
        _cache_key(scope, key),
        {"status": status_code, "data": data, "payload_hash": payload_hash},
        IDEMPOTENCY_TTL_SECONDS,
    )


def acquire_processing_lock(scope, key):
    """Mark an idempotency key as being processed.

    Args:
        scope (str | int): the acting caller's scope.
        key (str): the idempotency key.

    Returns:
        bool: True if this caller won the lock, False if another request
            with the same key is already being processed.
    """
    return cache.add(_cache_key(scope, key) + ":lock", "1", PROCESSING_LOCK_TTL_SECONDS)


def release_processing_lock(scope, key):
    """Clear the in-progress marker for an idempotency key.

    Args:
        scope (str | int): the acting caller's scope.
        key (str): the idempotency key.
    """
    cache.delete(_cache_key(scope, key) + ":lock")


def request_fingerprint(request):
    """Return a stable hash of what the request carries.

    The fingerprint binds an idempotency key to the request it first
    succeeded with: method + path + body for regular posts, and field names
    plus file contents for multipart uploads (whose raw body carries a random
    boundary on every retry and would otherwise never match itself).

    Args:
        request: the incoming request.

    Returns:
        str: the hex digest identifying this request's payload.
    """
    hasher = hashlib.sha256()
    hasher.update(request.method.encode("utf-8"))
    hasher.update(request.path.encode("utf-8"))
    content_type = request.META.get("CONTENT_TYPE", "")
    if "multipart" in content_type:
        for name in sorted(request.POST):
            hasher.update(str(name).encode("utf-8"))
            hasher.update(str(request.POST[name]).encode("utf-8"))
        for name in sorted(request.FILES):
            uploaded = request.FILES[name]
            hasher.update(str(name).encode("utf-8"))
            hasher.update((uploaded.name or "").encode("utf-8"))
            for chunk in uploaded.chunks():
                hasher.update(chunk)
            uploaded.seek(0)
    else:
        hasher.update(request.body or b"")
    return hasher.hexdigest()


def payload_conflict(cached, payload_hash):
    """Return whether a cached result belongs to a different request.

    Entries stored before fingerprinting carry no hash and never conflict,
    so a deploy does not invalidate keys already in flight.

    Args:
        cached (dict | None): the stored ``{"status", "data",
            "payload_hash"}`` result, if any.
        payload_hash (str): fingerprint of the current request.

    Returns:
        bool: True when the key was already used with a different payload.
    """
    if not cached:
        return False
    stored = cached.get("payload_hash")
    return stored is not None and stored != payload_hash


def conflicting_key_response():
    """Return the 409 response for a key reused with a different payload.

    Returns:
        Response: ``409 Conflict`` telling the caller to mint a fresh key.
    """
    return Response(
        {
            "detail": (
                "This Idempotency-Key was already used with a different "
                "request. Use a new key for a different request."
            )
        },
        status=http_status.HTTP_409_CONFLICT,
    )


class IdempotentCreateMixin:
    """Mixin for create views that must reject duplicate mutations.

    Requires the ``Idempotency-Key`` header, replays the stored response for
    a repeat of a key that already succeeded, and returns 409 while an
    earlier request with the same key is still being processed.
    """

    def create(self, request, *args, **kwargs):
        """Enforce idempotency around the standard create behaviour.

        Args:
            request: the incoming request.

        Returns:
            Response: the replayed or freshly created response.
        """
        key = require_idempotency_key(request)
        scope = f"u{request.user.pk}"
        fingerprint = request_fingerprint(request)
        cached = read_cached_result(scope, key)
        if cached is not None:
            if payload_conflict(cached, fingerprint):
                return conflicting_key_response()
            return Response(cached["data"], status=cached["status"])
        if not acquire_processing_lock(scope, key):
            return Response(
                {
                    "detail": "A request with this Idempotency-Key is already in progress."
                },
                status=http_status.HTTP_409_CONFLICT,
            )
        try:
            response = super().create(request, *args, **kwargs)
            if response.status_code in IDEMPOTENT_STATUS_CODES:
                store_result(
                    scope, key, response.status_code, response.data, fingerprint
                )
            return response
        finally:
            release_processing_lock(scope, key)
