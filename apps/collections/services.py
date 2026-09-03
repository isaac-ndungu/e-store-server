"""Business logic for the collections app.

The smart-collection refresh is the core here. A smart ``Collection`` is
governed by one rule that decides which products belong to it:

- ``new_arrivals`` — products created within the rule window, newest first.
- ``restocked`` — products with a restock recorded within the window.
- ``on_sale`` — products with a currently active automatic discount.
- ``low_stock`` (almost gone) — products whose available stock across all
  warehouses is at or below ``rule_threshold``.

Each refresh deletes the collection's previous smart membership rows and
recreates them from the freshly computed product set inside one transaction,
then caches the resulting product id list per slug. Manual collections are
untouched. Collecting the product ids in a single query and matching them in
Python keeps the refresh cheap even with many candidates.

``best_sellers`` is a valid smart rule on the model, but its data source —
order-line counts — is not yet built, so it currently computes to an empty
membership. Its computation slots into ``_SMART_RULE_COMPUTERS`` when that
feature lands.
"""

import logging

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.collections import cache
from apps.collections.models import Collection, CollectionMembership
from apps.core.models import SiteConfig

logger = logging.getLogger(__name__)


def _active_window(collection):
    """Decide whether a collection is currently live for a refresh.

    A collection is live when it is active and ``now`` falls inside its
    configured ``starts_at`` / ``ends_at`` range, with an unset bound treated
    as open-ended.

    Args:
        collection (Collection): the collection.

    Returns:
        tuple: ``(in_window, error)`` where ``in_window`` is bool and
            ``error`` is None (or a reason string when not in window).
    """
    now = timezone.now()
    if not collection.is_active:
        return False, "collection is not active"
    if collection.starts_at and collection.starts_at > now:
        return False, f"collection starts at {collection.starts_at}"
    if collection.ends_at and collection.ends_at < now:
        return False, f"collection ended at {collection.ends_at}"
    return True, None


def _window_start(rule_window_days):
    """Return the datetime marking the start of the rule window.

    Args:
        rule_window_days (int): the number of days in the window.

    Returns:
        datetime: the window start, timezone-aware.
    """
    return timezone.now() - timezone.timedelta(days=rule_window_days)


def _new_arrivals(collection):
    """Return products created within the collection's rule window.

    Args:
        collection (Collection): the smart collection.

    Returns:
        list[int]: product primary keys, newest first.
    """
    from apps.catalog.models import Product

    start = _window_start(collection.rule_window_days)
    return list(
        Product.objects.filter(is_active=True, created_at__gte=start)
        .order_by("-created_at")
        .values_list("pk", flat=True)
    )


def _restocked(collection):
    """Return products restocked within the collection's rule window.

    ``last_restocked_at`` is stamped when stock is received; the rule surfaces
    recent re-stocks so shoppers see what is newly available.

    Args:
        collection (Collection): the smart collection.

    Returns:
        list[int]: product primary keys, most recently restocked first.
    """
    from apps.catalog.models import Product

    start = _window_start(collection.rule_window_days)
    return list(
        Product.objects.filter(
            is_active=True,
            last_restocked_at__isnull=False,
            last_restocked_at__gte=start,
        )
        .order_by("-last_restocked_at")
        .values_list("pk", flat=True)
    )


def _low_stock(collection):
    """Return products with a variant running low on stock.

    Available stock is summed across all warehouses for each variant; a
    product qualifies when any of its active variants has at most
    ``rule_threshold`` units available — i.e. the product is almost gone.
    The default threshold is the configured low-stock level.

    Args:
        collection (Collection): the smart collection.

    Returns:
        list[int]: product primary keys.
    """
    from apps.catalog.models import Product, ProductVariant
    from apps.inventory.models import Inventory

    threshold = collection.rule_threshold
    if threshold is None:
        threshold = SiteConfig.load().settings.get("low_stock_threshold", 5)

    available_by_variant = dict(
        Inventory.objects.filter(warehouse__is_active=True)
        .values("variant_id")
        .annotate(available=Sum("quantity") - Sum("reserved"))
        .values_list("variant_id", "available")
    )
    low_pks = [
        variant_id
        for variant_id, available in available_by_variant.items()
        if int(available) <= threshold
    ]
    if not low_pks:
        return []
    return list(
        Product.objects.filter(
            is_active=True,
            variants__in=ProductVariant.objects.filter(pk__in=low_pks),
        )
        .distinct()
        .values_list("pk", flat=True)
    )


def _on_sale(collection):
    """Return products with a currently active discount.

    A product qualifies when at least one active, in-window discount applies
    to it under any scope other than ``bundle`` — a bundle discount reduces
    the bundle's own price, not the price of the products inside it. The
    result is ordered by discount priority so the most prominent offers come
    first.

    Args:
        collection (Collection): the smart collection.

    Returns:
        list[int]: active product primary keys currently on sale.
    """
    from django.db.models import Q

    from apps.catalog.models import Product
    from apps.promotions.models import Discount

    now = timezone.now()
    discounts = list(
        Discount.objects.filter(is_active=True, starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
        .exclude(scope="bundle")
        .order_by("-priority", "pk")
    )
    budgets = [
        discount
        for discount in discounts
        if discount.max_redemptions is None
        or discount.redemption_count < discount.max_redemptions
    ]

    product_pks = []
    seen = set()
    for discount in budgets:
        scope = discount.scope
        if scope == "variant":
            ids = discount.variants.values_list("product_id", flat=True)
        elif scope == "product":
            ids = discount.products.values_list("pk", flat=True)
        elif scope == "category":
            ids = discount.categories.values_list("products__pk", flat=True)
        elif scope == "brand":
            ids = discount.brands.values_list("products__pk", flat=True)
        else:
            ids = Product.objects.filter(is_active=True).values_list("pk", flat=True)
        for product_id in ids:
            if product_id is not None and product_id not in seen:
                seen.add(product_id)
                product_pks.append(product_id)
    if not product_pks:
        return []
    return list(
        Product.objects.filter(pk__in=product_pks, is_active=True)
        .order_by("-pk")
        .values_list("pk", flat=True)
    )


def _no_source(collection):
    """Return an empty membership for rules whose data source is pending.

    This stands in for ``on_sale`` and ``best_sellers`` until active-discount
    data and order-line counts are built; it is never invoked for a
    computable rule.

    Args:
        collection (Collection): the smart collection.

    Returns:
        list[int]: an empty list.
    """
    logger.warning(
        "Smart rule %r is not yet computable; skipping.", collection.smart_rule
    )
    return []


_SMART_RULE_COMPUTERS = {
    "new_arrivals": _new_arrivals,
    "restocked": _restocked,
    "low_stock": _low_stock,
    "on_sale": _on_sale,
    "best_sellers": _no_source,
}


def compute_membership(collection):
    """Return the product ids a smart collection currently matches.

    Args:
        collection (Collection): the smart collection to compute for.

    Returns:
        list[int]: the matching product primary keys, in display order.

    Raises:
        ValueError: if the collection is not a smart, rule-driven collection,
            or carries a rule with no registered computer.
    """
    if collection.collection_type != "smart" or not collection.smart_rule:
        raise ValueError("membership can only be computed for a smart collection")
    computer = _SMART_RULE_COMPUTERS.get(collection.smart_rule)
    if computer is None:
        raise ValueError(f"no membership computer for rule {collection.smart_rule!r}")
    return computer(collection)


def refresh_smart_collection(collection):
    """Replace a smart collection's membership with its current rule result.

    Deletes the existing membership rows and recreates them from the freshly
    computed product set, all in one transaction, then caches the new product
    id list. Idempotent: a retried refresh simply recomputes to the same
    membership. Manual collections are left untouched (returns False).

    Args:
        collection (Collection): the smart collection to refresh.

    Returns:
        bool: True if the collection was refreshed, False if it was manual or
            outside its active window.
    """
    if collection.collection_type != "smart":
        return False
    in_window, error = _active_window(collection)
    if not in_window:
        logger.info("Skipping refresh for %s: %s", collection.slug, error)
        return False

    product_pks = compute_membership(collection)

    with transaction.atomic():
        collection.memberships.all().delete()
        CollectionMembership.objects.bulk_create(
            [
                CollectionMembership(
                    collection=collection, product_id=pk, sort_order=index
                )
                for index, pk in enumerate(product_pks)
            ]
        )
    cache.cache_product_pks(collection.slug, product_pks)
    return True


def refresh_all_smart_collections():
    """Refresh every smart collection that is currently in its active window.

    Returns:
        int: the number of smart collections refreshed.
    """
    refreshed = 0
    for collection in Collection.objects.filter(
        collection_type="smart", is_active=True
    ):
        if refresh_smart_collection(collection):
            refreshed += 1
    return refreshed
