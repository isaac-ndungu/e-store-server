"""Business logic for the social proof app.

Recording a product view has two effects: the session is added to the
product's Redis live-viewer set (the urgency signal the storefront shows) and
a durable ``ProductViewEvent`` row is appended for analytics. The Redis write
is what the response carries; the row append is bucketed so a single session's
rapid reloads collapse into one event per five-minute window. Both are skipped
when social proof is switched off via ``SiteConfig``.
"""

import uuid

from django.utils import timezone

from apps.social_proof import cache
from apps.social_proof.constants import VIEW_EVENT_BUCKET_MINUTES
from apps.social_proof.models import ProductViewEvent


def _bucket_start(now=None):
    """Return the start of the view-event bucket a timestamp falls into.

    Bucket boundaries land on multiples of five minutes (00:00, 00:05, ...) so
    rows from the same browsing burst share one key and deduplicate on the
    unique constraint.

    Args:
        now (datetime | None): the timestamp to bucket; defaults to now.

    Returns:
        datetime: the start of the enclosing five-minute bucket.
    """
    moment = now or timezone.now()
    return moment.replace(
        minute=(moment.minute // VIEW_EVENT_BUCKET_MINUTES) * VIEW_EVENT_BUCKET_MINUTES,
        second=0,
        microsecond=0,
    )


def _feature_enabled():
    """Return whether the social-proof feature is switched on.

    Returns:
        bool: the cached ``enable_social_proof`` setting, defaulting to True.
    """
    return cache.is_feature_enabled()


def is_feature_enabled():
    """Return whether the social-proof feature is switched on (public gate).

    Exposed so views can short-circuit an entire response (e.g. an empty
    recent-sales feed) without running a selector for data that must not be
    surfaced while the feature is off.

    Returns:
        bool: the cached ``enable_social_proof`` setting, defaulting to True.
    """
    return _feature_enabled()


def new_session_key():
    """Return a generated session identifier for cookie-less clients.

    Returns:
        str: a random hex identifier.
    """
    return uuid.uuid4().hex


def record_product_view(product, session_key=None):
    """Record a product view and return the updated live-viewer count.

    Adds the session to the product's Redis live-viewer set and upserts a
    durable ``ProductViewEvent`` row keyed by product, session, and the
    enclosing five-minute bucket, so retries and rapid reloads never inflate
    the analytics history. When social proof is disabled the call is a no-op
    that returns 0.

    Args:
        product (Product): the product viewed.
        session_key (str | None): the browsing session identifier; a fresh key
            is generated when omitted.

    Returns:
        int: the distinct live-viewer count after recording.
    """
    if not _feature_enabled():
        return 0
    if not session_key:
        session_key = new_session_key()
    cache.add_live_viewer(product.pk, session_key)
    ProductViewEvent.objects.get_or_create(
        product=product,
        session_key=session_key,
        bucket_started_at=_bucket_start(),
    )
    return cache.get_live_viewer_count(product.pk)


def get_live_viewer_count(product):
    """Return the current live-viewer count for a product.

    Args:
        product (Product): the product.

    Returns:
        int: the distinct live-viewer count, or 0 when social proof is
            disabled.
    """
    if not _feature_enabled():
        return 0
    return cache.get_live_viewer_count(product.pk)


def get_live_viewer_counts(products):
    """Return live-viewer counts for a set of products at once.

    Args:
        products (iterable of Product): the products.

    Returns:
        dict: ``{product.pk: count}`` for each requested product, 0 for each
            when social proof is disabled.
    """
    if not _feature_enabled():
        return {product.pk: 0 for product in products}
    return cache.get_live_viewer_count_batch([product.pk for product in products])
