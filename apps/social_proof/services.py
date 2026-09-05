"""Business logic for the social proof app.

Recording a product view has two effects: the session is added to the
product's Redis live-viewer set (the urgency signal the storefront shows) and
a durable ``ProductViewEvent`` row is appended for analytics. The Redis write
is what the response carries; the row append is a small forward-only insert
that keeps the event history queryable. Both are skipped when social proof is
switched off via ``SiteConfig``.
"""

import uuid

from apps.core.models import SiteConfig
from apps.social_proof import cache
from apps.social_proof.models import ProductViewEvent


def _feature_enabled():
    """Return whether the social-proof feature is switched on.

    Returns:
        bool: the ``enable_social_proof`` setting, defaulting to True.
    """
    return SiteConfig.load().settings.get("enable_social_proof", True)


def new_session_key():
    """Return a generated session identifier for cookie-less clients.

    Returns:
        str: a random hex identifier.
    """
    return uuid.uuid4().hex


def record_product_view(product, session_key=None):
    """Record a product view and return the updated live-viewer count.

    Adds the session to the product's Redis live-viewer set and appends a
    durable ``ProductViewEvent`` row. Idempotent per session within the live
    window: repeated views by the same session do not inflate the live count,
    though each view still appends one event row. When social proof is
    disabled the call is a no-op that returns 0.

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
    ProductViewEvent.objects.create(product=product, session_key=session_key)
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
