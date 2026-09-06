"""Read-side composition and aggregation helpers for the dashboard app.

The dashboard is the staff-facing widget layer: each function returns the
specific curated shape a UI card needs — thresholds, alert flags, named
breakdowns — rather than the generic, query-param-driven shape the analytics
app produces. Where a dashboard card has an exact analytics equivalent
(sales, stock, products, promotions, returns, support) the widget function
delegates to ``apps.analytics.selectors`` and adds only the widget-specific
bits; everything else is computed directly from the owning app's tables. No
module duplicates business logic that already lives behind the analytics
aggregates, and no module reads a table that does not exist — a section whose
domain has no backing store is simply not surfaced.

Money is summed as ``Decimal`` throughout, mirroring the analytics
conventions, and dashboard thresholds are module-level constants so tests can
hit the exact boundary either side of an alert window.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    Count,
    DecimalField,
    F,
    IntegerField,
    Q,
    Sum,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.analytics.selectors import (
    apply_period,
    orders_by_status,
    product_performance,
    promotions_summary,
    returns_summary,
    sales_summary,
    stock_snapshot,
    support_summary,
    traffic_summary,
)
from apps.bundles.models import Bundle
from apps.catalog.models import Product
from apps.collections.models import Collection
from apps.inventory.models import Inventory, StockReservation
from apps.orders.models import Order, OrderItem, OrderVerification
from apps.promotions.models import Coupon, Discount
from apps.returns.models import ReturnRequest
from apps.shipping.models import DeliveryZone, WarehouseZonePriority
from apps.support.models import Ticket

# Order statuses excluded from the kept-order base, matching the analytics
# definition: cancelled/refunded orders are not revenue the business keeps.
_KEPT_ORDER_Q = ~Q(status__in=["cancelled", "refunded"])

# Alert and staleness windows, in one place so dashboard tests can seed rows a
# minute either side of the boundary and assert the edge behaviour.
_RESERVATION_EXPIRY_WINDOW = timedelta(minutes=15)
_COLLECTION_STALE_AGE = timedelta(minutes=30)
_PROMOTION_EXPIRING_WINDOW = timedelta(hours=48)
_SUPPORT_OLD_TICKET_AGE = timedelta(hours=24)
_RETURN_STUCK_AGE = timedelta(hours=72)
_ORDER_UNCONFIRMED_AGE = timedelta(minutes=30)
_COD_FAILURE_WINDOW = timedelta(hours=24)

_OPEN_ORDER_STATUSES = ("pending", "confirmed", "processing", "shipped")
_RESOLUTION_REQUIRED_STATUSES = ("requested", "approved", "item_received")


def _as_percent(numerator, denominator):
    """Return a portion as a two-decimal percentage, guarding division by zero.

    Args:
        numerator (int): the part.
        denominator (int): the whole.

    Returns:
        Decimal: the percentage, or ``Decimal("0.00")`` when the whole is zero.
    """
    if not denominator:
        return Decimal("0.00")
    return (Decimal(numerator) / Decimal(denominator) * Decimal(100)).quantize(
        Decimal("0.01")
    )


def _average_hours(elapsed_seconds):
    """Return the mean of a sequence of seconds as a two-decimal hour value.

    Args:
        elapsed_seconds (list): seconds per observation, possibly empty.

    Returns:
        Decimal: the average in hours, or ``Decimal("0.00")`` when empty.
    """
    if not elapsed_seconds:
        return Decimal("0.00")
    total_seconds = sum((Decimal(value) for value in elapsed_seconds), Decimal(0))
    raw_average = total_seconds / Decimal(len(elapsed_seconds))
    return (raw_average / 3600).quantize(Decimal("0.01"))


def sales_dashboard(from_time=None, to_time=None):
    """Return the sales-overview widget for the period.

    Composes the analytics sales aggregate and orders-by-status breakdown, and
    adds a conversion figure: the share of recorded product views that ended
    in a delivered order. The rate is a percentage to two decimal places.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: the sales widget with ``summary``, ``orders_by_status``, and
            ``conversion``.
    """
    views = traffic_summary(from_time, to_time)["view_count"]
    delivered = apply_period(
        Order.objects.filter(status="delivered"), from_time, to_time
    ).count()
    return {
        "summary": sales_summary(from_time, to_time),
        "orders_by_status": orders_by_status(from_time, to_time),
        "conversion": {
            "views": views,
            "delivered": delivered,
            "delivered_rate_percent": _as_percent(delivered, views),
        },
    }


def cod_dashboard():
    """Return the COD-operations widget for the current state.

    COD is tracked through two existing sources — ``OrderVerification`` rows
    (the delivery OTP flow) and COD orders their own statuses — so the widget
    is a live split of both rather than a per-collection register that does
    not yet exist.

    Returns:
        dict: ``verifications`` split by status and ``orders`` split by
            fulfilment state.
    """
    verifications = OrderVerification.objects.all()
    cod_orders = Order.objects.filter(payment_method="cod")
    return {
        "verifications": {
            "total": verifications.count(),
            "pending": verifications.filter(status="pending").count(),
            "verified": verifications.filter(status="verified").count(),
            "expired": verifications.filter(status="expired").count(),
            "failed": verifications.filter(status="failed").count(),
        },
        "orders": {
            "total": cod_orders.count(),
            "open": cod_orders.filter(status__in=_OPEN_ORDER_STATUSES).count(),
            "delivered": cod_orders.filter(status="delivered").count(),
            "delivery_failed": cod_orders.filter(status="delivery_failed").count(),
        },
    }


def stock_dashboard():
    """Return the stock-and-reservations widget for the current state.

    Wraps the analytics stock snapshot and adds the reservation pipeline:
    how many active reservations are close to expiring (within the sweep
    grace window) versus how many have already passed their expiry and are
    waiting on the next sweep to be released.

    Returns:
        dict: the stock snapshot plus a ``reservations`` breakdown.
    """
    now = timezone.now()
    active = StockReservation.objects.filter(status="active")
    window_end = now + _RESERVATION_EXPIRY_WINDOW
    return {
        "snapshot": stock_snapshot(),
        "reservations": {
            "active": active.count(),
            "expiry_window_minutes": int(
                _RESERVATION_EXPIRY_WINDOW.total_seconds() // 60
            ),
            "expiring_soon": active.filter(
                expires_at__gte=now, expires_at__lte=window_end
            ).count(),
            "overdue_unreleased": active.filter(expires_at__lt=now).count(),
        },
    }


def products_dashboard(from_time=None, to_time=None, limit=10):
    """Return the product-performance widget for the period.

    Composes the analytics top-products list and adds catalogue-health flags:
    which discontinued products have no replacement lined up, so a buyer
    browsing them has nowhere to go.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.
        limit (int): the maximum number of top products to return.

    Returns:
        dict: ``top`` products, ``discontinued_without_replacement`` rows,
            and ``catalogue`` counts.
    """
    orphaned = list(
        Product.objects.filter(
            is_discontinued=True, replacement_product__isnull=True
        ).only("sku", "name")
    )
    return {
        "top": product_performance(from_time, to_time, limit=limit),
        "discontinued_without_replacement": [
            {"sku": product.sku, "name": product.name} for product in orphaned
        ],
        "catalogue": {
            "active": Product.objects.filter(is_active=True).count(),
            "discontinued": Product.objects.filter(is_discontinued=True).count(),
            "discontinued_without_replacement": len(orphaned),
        },
    }


def collections_dashboard():
    """Return the smart-collection refresh-status widget.

    A smart collection is ``is_stale`` when its refresh has missed several
    beats: either it has never run (``last_refreshed_at`` unset) or its last
    run is older than the staleness threshold.

    Returns:
        dict: per-collection refresh rows plus aggregate staleness counts.
    """
    now = timezone.now()
    threshold_minutes = int(_COLLECTION_STALE_AGE.total_seconds() // 60)
    rows = Collection.objects.annotate(
        member_count=Count("memberships", distinct=True)
    ).order_by("sort_order", "name")
    entries = []
    stale_count = 0
    for collection in rows:
        if collection.collection_type != "smart":
            entries.append(
                {
                    "slug": collection.slug,
                    "name": collection.name,
                    "collection_type": collection.collection_type,
                    "smart_rule": collection.smart_rule,
                    "is_active": collection.is_active,
                    "member_count": collection.member_count,
                    "last_refreshed_at": None,
                    "refresh_age_minutes": None,
                    "is_stale": False,
                }
            )
            continue
        if collection.last_refreshed_at is None:
            is_stale = True
            age_minutes = None
        else:
            age_minutes = int(
                (now - collection.last_refreshed_at).total_seconds() // 60
            )
            is_stale = age_minutes > threshold_minutes
        if is_stale:
            stale_count += 1
        entries.append(
            {
                "slug": collection.slug,
                "name": collection.name,
                "collection_type": collection.collection_type,
                "smart_rule": collection.smart_rule,
                "is_active": collection.is_active,
                "member_count": collection.member_count,
                "last_refreshed_at": (
                    collection.last_refreshed_at.isoformat()
                    if collection.last_refreshed_at
                    else None
                ),
                "refresh_age_minutes": age_minutes,
                "is_stale": is_stale,
            }
        )
    return {
        "stale_threshold_minutes": threshold_minutes,
        "smart_count": Collection.objects.filter(collection_type="smart").count(),
        "manual_count": Collection.objects.filter(collection_type="manual").count(),
        "stale_count": stale_count,
        "collections": entries,
    }


def bundles_dashboard(from_time=None, to_time=None):
    """Return the bundle-performance widget for the period.

    Bundle purchases are the set of distinct ``bundle_group_id`` values seen
    on kept order items in the period; ``attach_rate_percent`` is their share
    of all kept orders, and ``discount_cost`` is what the platform gave away —
    the difference between the component prices a customer would have paid
    standalone and what the bundle actually charged.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: per-bundle figures plus overall purchase and discount-cost sums.
    """
    item_orders = apply_period(Order.objects.filter(_KEPT_ORDER_Q), from_time, to_time)
    items = OrderItem.objects.filter(
        order__in=item_orders, bundle__isnull=False, bundle_group_id__isnull=False
    )
    rows = (
        items.values("bundle__slug", "bundle__name", "bundle__is_active")
        .annotate(
            purchases=Count("bundle_group_id", distinct=True),
            units=Sum("quantity", output_field=IntegerField()),
            revenue=Coalesce(
                Sum(
                    "total_price",
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
            gross_before=Coalesce(
                Sum(
                    F("unit_price") * F("quantity"),
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
        )
        .order_by("bundle__slug")
    )
    bundles = []
    total_purchases = 0
    total_gross = Decimal(0)
    total_revenue = Decimal(0)
    for row in rows:
        discount_given = row["gross_before"] - row["revenue"]
        bundles.append(
            {
                "slug": row["bundle__slug"],
                "name": row["bundle__name"],
                "is_active": row["bundle__is_active"],
                "purchases": row["purchases"],
                "units": row["units"] or 0,
                "revenue": row["revenue"],
                "discount_given": discount_given,
            }
        )
        total_purchases += row["purchases"]
        total_gross += row["gross_before"]
        total_revenue += row["revenue"]
    order_count = sales_summary(from_time, to_time)["order_count"]
    return {
        "active_bundles": Bundle.objects.filter(is_active=True).count(),
        "bundle_orders": total_purchases,
        "total_orders": order_count,
        "attach_rate_percent": _as_percent(total_purchases, order_count),
        "discount_cost": total_gross - total_revenue,
        "bundles": bundles,
    }


def promotions_dashboard(from_time=None, to_time=None):
    """Return the promotions widget with recently-expiring campaigns.

    Composes the analytics promotions aggregate and lists discounts and
    coupons whose end is within the expiring window, so staff can budget a
    deadline rather than rediscover it at the end.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: the promotions aggregate plus ``expiring_soon`` entries.
    """
    now = timezone.now()
    window = _PROMOTION_EXPIRING_WINDOW
    window_end = now + window
    expiring_entries = []

    discounts = Discount.objects.filter(
        is_active=True, ends_at__isnull=False, ends_at__gt=now, ends_at__lte=window_end
    ).only("name", "ends_at")
    coupons = Coupon.objects.filter(
        is_active=True, ends_at__isnull=False, ends_at__gt=now, ends_at__lte=window_end
    ).only("code", "ends_at")

    for discount in discounts:
        hours_left = Decimal((discount.ends_at - now).total_seconds()) / 3600
        expiring_entries.append(
            {
                "kind": "discount",
                "label": discount.name,
                "ends_at": discount.ends_at.isoformat(),
                "hours_remaining": Decimal(hours_left).quantize(Decimal("0.1")),
            }
        )
    for coupon in coupons:
        hours_left = Decimal((coupon.ends_at - now).total_seconds()) / 3600
        expiring_entries.append(
            {
                "kind": "coupon",
                "label": coupon.code,
                "ends_at": coupon.ends_at.isoformat(),
                "hours_remaining": Decimal(hours_left).quantize(Decimal("0.1")),
            }
        )

    return {
        "summary": promotions_summary(from_time, to_time),
        "expiring_window_hours": int(window.total_seconds() // 3600),
        "expiring_soon": expiring_entries,
        "expiring_soon_count": len(expiring_entries),
    }


def returns_dashboard(from_time=None, to_time=None):
    """Return the returns/RMA widget with a resolution-time gauge.

    Composes the analytics returns aggregate and adds how long resolved
    requests took from opening to resolution, cross-checked against return
    requests still waiting beyond a stuck threshold.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: the returns aggregate plus a ``resolution`` breakdown.
    """
    opened_in_period = apply_period(
        ReturnRequest.objects.all(), from_time, to_time, field="created_at"
    )
    resolved_requests = list(
        opened_in_period.filter(resolved_at__isnull=False).only(
            "created_at", "resolved_at"
        )
    )
    resolved_seconds = [
        (request.resolved_at - request.created_at).total_seconds()
        for request in resolved_requests
    ]
    now = timezone.now()
    stuck = ReturnRequest.objects.filter(
        status__in=_RESOLUTION_REQUIRED_STATUSES,
        created_at__lt=now - _RETURN_STUCK_AGE,
    ).count()
    return {
        "summary": returns_summary(from_time, to_time),
        "resolution": {
            "resolved": len(resolved_requests),
            "avg_resolution_hours": _average_hours(resolved_seconds),
            "stuck_threshold_hours": int(_RETURN_STUCK_AGE.total_seconds() // 3600),
            "stuck_open_over_threshold": stuck,
        },
    }


def warehouse_routing_dashboard():
    """Return the warehouse-routing coverage widget for the current state.

    A delivery zone is covered when it has at least one configured
    ``WarehouseZonePriority`` row; zones with none fall back to an
    unranked warehouse guess and are surfaced as routing gaps.

    Returns:
        dict: zone totals, coverage percentage, and the gap list.
    """
    active_zones = list(
        DeliveryZone.objects.filter(is_active=True).only("pk", "county", "area_name")
    )
    configured = set(
        WarehouseZonePriority.objects.values_list("delivery_zone_id", flat=True)
    )
    gaps = [
        {"county": zone.county, "area_name": zone.area_name}
        for zone in active_zones
        if zone.pk not in configured
    ]
    total = len(active_zones)
    covered = total - len(gaps)
    return {
        "zones_total": total,
        "zones_covered": covered,
        "coverage_percent": _as_percent(covered, total),
        "gaps": gaps,
    }


def support_dashboard(from_time=None, to_time=None):
    """Return the support widget with an age breakdown of the open queue.

    Composes the analytics support aggregate (status and category splits) and
    adds the current open-queue age: the oldest open ticket, the average open
    age, and how many conversations have dragged past the old-ticket
    threshold.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: the support aggregate plus an ``age`` breakdown.
    """
    now = timezone.now()
    open_tickets = Ticket.objects.filter(status__in=("open", "pending_customer")).only(
        "created_at"
    )
    ages_seconds = [
        int((now - ticket.created_at).total_seconds()) for ticket in open_tickets
    ]
    oldest_hours = None
    if ages_seconds:
        oldest_hours = (Decimal(max(ages_seconds)) / 3600).quantize(Decimal("0.1"))
    return {
        "summary": support_summary(from_time, to_time),
        "age": {
            "old_ticket_threshold_hours": int(
                _SUPPORT_OLD_TICKET_AGE.total_seconds() // 3600
            ),
            "open_older_than_threshold": open_tickets.filter(
                created_at__lt=now - _SUPPORT_OLD_TICKET_AGE
            ).count(),
            "oldest_open_age_hours": oldest_hours,
            "avg_open_age_hours": _average_hours(ages_seconds),
        },
    }


def alerts():
    """Return the live alert feed, computed fresh on every call.

    This is a polling feed, not a push stream: the widget is cheap enough to
    re-read every 30–60 seconds, matching how the rest of the platform does
    real-time-ish work (Celery Beat sweeps plus live reads). Only alert groups
    with at least one hit are included, so a silence means the feed is empty
    rather than a payload of zeroes. Push over Channels/websockets is
    deliberately deferred.

    Returns:
        dict: ``generated_at`` and the list of active ``alerts``.
    """
    now = timezone.now()
    alerts = []

    stock_rows = list(
        Inventory.objects.annotate(available_units=F("quantity") - F("reserved"))
        .filter(available_units__lte=F("low_stock_threshold"))
        .select_related("variant__product", "warehouse")
        .order_by("available_units")
    )
    if stock_rows:
        out_of_stock = any(row.available_units == 0 for row in stock_rows)
        alerts.append(
            {
                "type": "low_stock",
                "severity": "critical" if out_of_stock else "warning",
                "count": len(stock_rows),
                "items": [
                    {
                        "label": (
                            f"{row.variant.sku} ({row.warehouse.name}) — "
                            f"{row.available_units} left"
                        )
                    }
                    for row in stock_rows[:8]
                ],
            }
        )

    expiring = StockReservation.objects.filter(
        status="active", expires_at__lte=now + _RESERVATION_EXPIRY_WINDOW
    ).select_related("inventory__variant", "order_item__order")
    expiring_count = expiring.count()
    if expiring_count:
        alerts.append(
            {
                "type": "reservation_expiring",
                "severity": "warning",
                "count": expiring_count,
                "items": [
                    {
                        "label": (
                            f"{reservation.inventory.variant.sku} reservation "
                            f"{reservation.pk} expires "
                            f"{reservation.expires_at.isoformat()}"
                        )
                    }
                    for reservation in expiring.order_by("expires_at")[:8]
                ],
            }
        )

    failed_cod = Order.objects.filter(
        payment_method="cod",
        status="delivery_failed",
        updated_at__gte=now - _COD_FAILURE_WINDOW,
    )
    failed_cod_count = failed_cod.count()
    if failed_cod_count:
        alerts.append(
            {
                "type": "failed_cod_deliveries",
                "severity": "warning",
                "count": failed_cod_count,
                "items": [
                    {"label": f"order {order.lookup_token}"}
                    for order in failed_cod.only("lookup_token").order_by(
                        "-updated_at"
                    )[:8]
                ],
            }
        )

    unconfirmed = Order.objects.filter(
        status="pending", placed_at__lt=now - _ORDER_UNCONFIRMED_AGE
    )
    unconfirmed_count = unconfirmed.count()
    if unconfirmed_count:
        alerts.append(
            {
                "type": "unconfirmed_orders",
                "severity": "warning",
                "count": unconfirmed_count,
                "items": [
                    {"label": f"order {order.lookup_token}"}
                    for order in unconfirmed.only("lookup_token").order_by("placed_at")[
                        :8
                    ]
                ],
            }
        )

    stale_smart = [
        collection
        for collection in Collection.objects.filter(collection_type="smart").only(
            "slug", "last_refreshed_at"
        )
        if collection.last_refreshed_at is None
        or collection.last_refreshed_at < now - _COLLECTION_STALE_AGE
    ]
    if stale_smart:
        alerts.append(
            {
                "type": "stale_collections",
                "severity": "info",
                "count": len(stale_smart),
                "items": [{"label": f"collection {c.slug}"} for c in stale_smart[:8]],
            }
        )

    return {
        "generated_at": now.isoformat(),
        "alerts": alerts,
    }
