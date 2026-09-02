"""Shared idempotency-key handling for mutating endpoints.

A client that repeats a request (a retried tap, a network-level redelivery)
must not re-run the mutation. The caller supplies an ``Idempotency-Key``
header; the first successful response is stored under that key and replayed
verbatim on later requests with the same key, while a request already being
processed under the same key is answered with 409 rather than being run twice.

Only successful responses are cached — a failed (validation) attempt can be
retried with the same key after the caller fixes the payload.
"""

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


def _cache_key(user_pk, key):
    """Return the storage key for a user + idempotency key pair.

    Args:
        user_pk (int): the acting user's primary key.
        key (str): the idempotency key.

    Returns:
        str: the cache key.
    """
    return f"idempotency:{user_pk}:{key}"


def read_cached_result(user_pk, key):
    """Return a previously stored response, if any.

    Args:
        user_pk (int): the acting user's primary key.
        key (str): the idempotency key.

    Returns:
        dict | None: ``{"status": int, "data": dict}`` for a prior success,
            else ``None``.
    """
    return cache.get(_cache_key(user_pk, key))


def store_result(user_pk, key, status_code, data):
    """Persist a successful response under an idempotency key.

    Args:
        user_pk (int): the acting user's primary key.
        key (str): the idempotency key.
        status_code (int): the HTTP status to replay.
        data: the response payload to replay.
    """
    cache.set(
        _cache_key(user_pk, key),
        {"status": status_code, "data": data},
        IDEMPOTENCY_TTL_SECONDS,
    )


def acquire_processing_lock(user_pk, key):
    """Mark an idempotency key as being processed.

    Args:
        user_pk (int): the acting user's primary key.
        key (str): the idempotency key.

    Returns:
        bool: True if this caller won the lock, False if another request
            with the same key is already being processed.
    """
    return cache.add(
        _cache_key(user_pk, key) + ":lock", "1", PROCESSING_LOCK_TTL_SECONDS
    )


def release_processing_lock(user_pk, key):
    """Clear the in-progress marker for an idempotency key.

    Args:
        user_pk (int): the acting user's primary key.
        key (str): the idempotency key.
    """
    cache.delete(_cache_key(user_pk, key) + ":lock")


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
        cached = read_cached_result(request.user.pk, key)
        if cached is not None:
            return Response(cached["data"], status=cached["status"])
        if not acquire_processing_lock(request.user.pk, key):
            return Response(
                {
                    "detail": "A request with this Idempotency-Key is already in progress."
                },
                status=http_status.HTTP_409_CONFLICT,
            )
        try:
            response = super().create(request, *args, **kwargs)
            if response.status_code in IDEMPOTENT_STATUS_CODES:
                store_result(request.user.pk, key, response.status_code, response.data)
            return response
        finally:
            release_processing_lock(request.user.pk, key)
