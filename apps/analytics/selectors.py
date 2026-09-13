"""Read-only aggregation helpers for the analytics app.

Every report here aggregates from the platform's source-of-truth tables —
orders, returns, promotions, support, reviews, social proof, and
notifications — so a dashboard figure always reconciles against the
underlying rows. Aggregations that sum money use ``Decimal`` exclusively (never
``float``) and time-series grouping is pushed into the database with Django's
``Trunc`` functions so a large table never fans out into one query per row.

The reports intentionally read only the tables that exist; sections whose
domain has no backing store yet (a loyalty ledger, COD collections,
eTIMS credit notes) are simply not reported rather than invented.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    Avg,
    Count,
    DecimalField,
    IntegerField,
    Q,
    Sum,
)
from django.db.models.functions import Coalesce, TruncDate, TruncMonth, TruncWeek
from django.utils import timezone

from apps.bundles.models import Bundle
from apps.catalog.models import Product, ProductVariant
from apps.inquiries.models import Inquiry
from apps.notifications.models import NotificationLog
from apps.orders.models import Order, OrderItem
from apps.promotions.models import CouponRedemption, Discount
from apps.returns.models import ReturnRequest
from apps.reviews.models import Review
from apps.social_proof.models import ProductViewEvent
from apps.support.models import Ticket

# Order statuses that represent money the business actually keeps. Cancelled and
# refunded orders are excluded from gross order value because the merchandise
# was not, or no longer, sold; every other status counts toward it.
_COMPLETED_ORDER_Q = ~Q(status__in=["cancelled", "refunded"])

_TRUNC_BY_GROUP = {
    "day": TruncDate("placed_at"),
    "week": TruncWeek("placed_at"),
    "month": TruncMonth("placed_at"),
}


def valid_groupings():
    """Return the supported time-series grouping keys.

    Returns:
        tuple: the ``group_by`` values accepted by the sales report.
    """
    return tuple(_TRUNC_BY_GROUP)


def apply_period(queryset, from_time, to_time, field="placed_at"):
    """Restrict a queryset to an inclusive bounding period on a datetime field.

    When both bounds are absent the queryset is returned unchanged, so an
    omitted range means "all time" and every figure stays internally
    consistent within a single request.

    Args:
        queryset (QuerySet): the queryset to filter.
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.
        field (str): the datetime field name to bound.

    Returns:
        QuerySet: the queryset bounded to the period.
    """
    lookup = {f"{field}__gte": from_time} if from_time else {}
    if to_time:
        # An inclusive ``to`` on a datetime column means the whole of that
        # instant, so add a microsecond and treat it as an exclusive upper
        # bound rather than trimming the column to seconds.
        lookup[f"{field}__lt"] = to_time + timedelta(microseconds=1)
    return queryset.filter(**lookup)


def sales_summary(from_time=None, to_time=None):
    """Return aggregate sales figures for the period.

    ``order_value`` sums the grand total of every non-cancelled, non-refunded
    order in the period; ``tax_collected`` adds order VAT to delivery VAT;
    ``collected`` equals order value — payment is arranged by staff before an
    order is created, so kept orders count as received; ``refund_outflow``
    sums the recorded refund amounts on cancelled/refunded orders; and
    ``net`` is collected minus refunds. Figures are ``Decimal``.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: the sales aggregate for the period.
    """
    orders = apply_period(Order.objects.all(), from_time, to_time)
    kept_orders = orders.filter(_COMPLETED_ORDER_Q)

    totals = kept_orders.aggregate(
        order_value=Coalesce(
            Sum(
                "grand_total",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        ),
        subtotal=Coalesce(
            Sum("subtotal", output_field=DecimalField(max_digits=20, decimal_places=2)),
            Decimal(0),
        ),
        shipping=Coalesce(
            Sum(
                "delivery_fee",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        ),
        discount=Coalesce(
            Sum(
                "discount_total",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        ),
        tax=Coalesce(
            Sum(
                "tax_total", output_field=DecimalField(max_digits=20, decimal_places=2)
            ),
            Decimal(0),
        ),
        shipping_tax=Coalesce(
            Sum(
                "shipping_tax_amount",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        ),
        order_count=Count("pk"),
    )

    collected = totals["order_value"]

    refund_outflow = apply_period(
        Order.objects.filter(status__in=("cancelled", "refunded")),
        from_time,
        to_time,
        field="placed_at",
    ).aggregate(
        value=Coalesce(
            Sum(
                "refund_amount",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        )
    )[
        "value"
    ]

    order_count = totals["order_count"]
    order_value = totals["order_value"]
    avg_order_value = order_value / order_count if order_count else Decimal("0")

    return {
        "order_count": order_count,
        "order_value": order_value,
        "avg_order_value": avg_order_value,
        "subtotal": totals["subtotal"],
        "shipping": totals["shipping"],
        "shipping_tax": totals["shipping_tax"],
        "discount": totals["discount"],
        "tax_collected": totals["tax"] + totals["shipping_tax"],
        "collected": collected,
        "refund_outflow": refund_outflow,
        "net": collected - refund_outflow,
    }


def orders_by_status(from_time=None, to_time=None):
    """Return order count and value grouped by status for the period.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        list: one ``{"status", "count", "value"}`` entry per status with orders
            in the period.
    """
    rows = (
        apply_period(Order.objects.all(), from_time, to_time)
        .values("status")
        .annotate(
            count=Count("pk"),
            value=Coalesce(
                Sum(
                    "grand_total",
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
        )
        .order_by("status")
    )
    return [
        {"status": row["status"], "count": row["count"], "value": row["value"]}
        for row in rows
    ]


def orders_by_source(from_time=None, to_time=None):
    """Return order count and value grouped by order source for the period.

    The source split (whatsapp/email/admin-manual) is the primary channel
    breakdown: every order is staff-created after a WhatsApp/email hand-off
    or entered manually, so this replaces any payment-method grouping.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        list: one ``{"source", "count", "value"}`` entry per source with
            orders in the period.
    """
    rows = (
        apply_period(Order.objects.all(), from_time, to_time)
        .values("order_source")
        .annotate(
            count=Count("pk"),
            value=Coalesce(
                Sum(
                    "grand_total",
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
        )
        .order_by("order_source")
    )
    return [
        {"source": row["order_source"], "count": row["count"], "value": row["value"]}
        for row in rows
    ]


def inquiries_summary(from_time=None, to_time=None):
    """Return inquiry capture and conversion figures for the period.

    ``total`` counts hand-offs logged in the period; ``by_channel`` and
    ``by_status`` split the same set; ``converted`` counts rows already
    linked to an order via the staff intake flow; ``conversion_rate_percent``
    is converted over total to two decimal places.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: inquiry aggregate for the period.
    """
    inquiries = apply_period(
        Inquiry.objects.all(), from_time, to_time, field="created_at"
    )
    total = inquiries.count()
    by_channel = list(
        inquiries.values("channel").annotate(count=Count("pk")).order_by("channel")
    )
    by_status = list(
        inquiries.values("status").annotate(count=Count("pk")).order_by("status")
    )
    converted = inquiries.filter(status="converted").count()
    if total:
        rate = (Decimal(converted) / Decimal(total) * Decimal(100)).quantize(
            Decimal("0.01")
        )
    else:
        rate = Decimal("0.00")
    return {
        "total": total,
        "by_channel": by_channel,
        "by_status": by_status,
        "converted": converted,
        "conversion_rate_percent": rate,
    }


def sales_timeseries(from_time=None, to_time=None, group_by="day"):
    """Return a bucketed sales series for the period.

    Each bucket carries order count, gross order value, tax, and average order
    value so a chart can plot volume and revenue on the same timeline.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.
        group_by (str): ``day``, ``week``, or ``month``.

    Returns:
        list: one ``{"bucket", "count", "value", "tax", "avg_order_value"}``
            entry per time bucket, ascending.
    """
    trunc = _TRUNC_BY_GROUP[group_by]
    queryset = (
        apply_period(Order.objects.filter(_COMPLETED_ORDER_Q), from_time, to_time)
        .annotate(bucket=trunc)
        .values("bucket")
        .annotate(
            count=Count("pk"),
            value=Coalesce(
                Sum(
                    "grand_total",
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
            tax=Coalesce(
                Sum(
                    "tax_total",
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
        )
        .order_by("bucket")
    )
    result = []
    for row in queryset:
        count = row["count"]
        value = row["value"]
        result.append(
            {
                "bucket": row["bucket"].isoformat(),
                "count": count,
                "value": value,
                "tax": row["tax"],
                "avg_order_value": value / count if count else Decimal("0"),
            }
        )
    return result


def stock_snapshot():
    """Return a current availability snapshot by staff-set stock status.

    Availability is a manual flag on each variant, not a counted quantity:
    the snapshot counts active variants per ``stock_status`` value, so staff
    see at a glance how much of the catalogue is sellable, running low, or
    off sale.

    Returns:
        dict: variant counts per stock status plus the active-variant total.
    """
    counts = (
        ProductVariant.objects.filter(is_active=True, product__is_active=True)
        .values("stock_status")
        .annotate(count=Count("pk"))
    )
    by_status = {row["stock_status"]: row["count"] for row in counts}
    in_stock = by_status.get("in_stock", 0)
    low_stock = by_status.get("low_stock", 0)
    out_of_stock = by_status.get("out_of_stock", 0)
    return {
        "in_stock": in_stock,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "variants": in_stock + low_stock + out_of_stock,
    }


def product_performance(from_time=None, to_time=None, limit=10):
    """Return the top products by revenue for the period.

    Revenue is computed from snapshotted ``OrderItem`` rows (never the live
    catalogue) for order lines on kept orders in the period. A deleted product
    contributes through its snapshotted name and sku. Each entry carries units
    sold, gross revenue, and the number of distinct orders the product appeared
    in.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.
        limit (int): the maximum number of products to return.

    Returns:
        list: the top products, highest revenue first.
    """
    item_orders = apply_period(
        Order.objects.filter(_COMPLETED_ORDER_Q), from_time, to_time
    )
    queryset = (
        OrderItem.objects.filter(order__in=item_orders)
        .values("product", "product_name", "variant_sku")
        .annotate(
            revenue=Coalesce(
                Sum(
                    "total_price",
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
                Decimal(0),
            ),
            units=Sum("quantity", output_field=IntegerField()),
            order_count=Count("order", distinct=True),
        )
        .order_by("-revenue")[:limit]
    )
    return [
        {
            "product_id": row["product"],
            "name": row["product_name"],
            "variant_sku": row["variant_sku"],
            "units": row["units"] or 0,
            "revenue": row["revenue"],
            "order_count": row["order_count"],
        }
        for row in queryset
    ]


def traffic_summary(from_time=None, to_time=None):
    """Return the number of recorded product views for the period.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: ``{"view_count": int}`` for the period.
    """
    return {
        "view_count": apply_period(
            ProductViewEvent.objects.all(), from_time, to_time, field="created_at"
        ).count()
    }


def promotions_summary(from_time=None, to_time=None):
    """Return promotion and coupon usage figures for the period.

    ``active_discounts`` counts discounts currently live (active and inside
    window), ``coupon_redemptions`` counts coupon uses in the period, and
    ``coupon_savings`` sums the discount total from orders that used a coupon in
    the period (an order that carries a coupon applied its code to that order).

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: promotion aggregate for the period.
    """
    now = timezone.now()
    active_discounts = Discount.objects.filter(
        is_active=True, starts_at__lte=now
    ).filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
    redemptions = apply_period(
        CouponRedemption.objects.all(), from_time, to_time, field="redeemed_at"
    )
    coupon_savings = apply_period(
        Order.objects.filter(_COMPLETED_ORDER_Q).filter(coupon__isnull=False),
        from_time,
        to_time,
    ).aggregate(
        savings=Coalesce(
            Sum(
                "discount_total",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        )
    )[
        "savings"
    ]
    return {
        "active_discounts": active_discounts.count(),
        "coupon_redemptions": redemptions.count(),
        "coupon_savings": coupon_savings,
    }


def returns_summary(from_time=None, to_time=None):
    """Return return-request figures for the period.

    ``refunded_amount`` sums the server-computed refund on refunded requests in
    the period; ``requested`` is the number of requests opened in the period.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: returns aggregate for the period.
    """
    requests = apply_period(
        ReturnRequest.objects.all(), from_time, to_time, field="created_at"
    )
    refunded_rows = requests.filter(status="refunded")
    refunded_amount = refunded_rows.aggregate(
        value=Coalesce(
            Sum(
                "refund_amount",
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            Decimal(0),
        )
    )["value"]
    return {
        "requested": requests.count(),
        "refunded": refunded_rows.count(),
        "refunded_amount": refunded_amount,
    }


def support_summary(from_time=None, to_time=None):
    """Return support-ticket figures for the period.

    Tickets opened, resolved, and currently open are counted, together with a
    breakdown by category so the queue's shape is visible at a glance.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: ``{"opened", "resolved", "open_now", "by_status", "by_category"}``.
    """
    opened = apply_period(Ticket.objects.all(), from_time, to_time, field="created_at")
    resolved = opened.filter(status__in=["resolved", "closed"]).count()
    open_now = Ticket.objects.filter(status__in=["open", "pending_customer"]).count()
    by_status = list(
        opened.values("status").annotate(count=Count("pk")).order_by("status")
    )
    by_category = list(
        opened.values("category").annotate(count=Count("pk")).order_by("category")
    )
    return {
        "opened": opened.count(),
        "resolved": resolved,
        "open_now": open_now,
        "by_status": by_status,
        "by_category": by_category,
    }


def reviews_summary(from_time=None, to_time=None):
    """Return review and rating figures for the period.

    ``count`` and ``avg_rating`` cover approved reviews created in the period;
    ``total_products`` is the number of catalogue products carrying at least one
    approved review.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: review aggregate for the period.
    """
    approved = apply_period(
        Review.objects.filter(is_approved=True), from_time, to_time, field="created_at"
    )
    aggregate = approved.aggregate(
        count=Count("pk"),
        avg_rating=Coalesce(
            Avg("rating", output_field=DecimalField(max_digits=5, decimal_places=2)),
            Decimal("0"),
        ),
    )
    return {
        "count": aggregate["count"],
        "avg_rating": aggregate["avg_rating"],
        "total_products": Product.objects.filter(review_count__gt=0).count(),
    }


def notifications_summary(from_time=None, to_time=None):
    """Return notification sends and failures for the period.

    ``count`` and ``failed`` cover logs created in the period (``failed`` is a
    break out of terminal-failed sends).

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: notification aggregate for the period.
    """
    logs = apply_period(
        NotificationLog.objects.all(), from_time, to_time, field="created_at"
    )
    return {
        "count": logs.count(),
        "failed": logs.filter(status="failed").count(),
    }


def catalogue_summary():
    """Return a current catalogue-size summary.

    Returns:
        dict: product, variant, and bundle counts.
    """
    return {
        "products": Product.objects.count(),
        "variants": ProductVariant.objects.count(),
        "bundles": Bundle.objects.filter(is_active=True).count(),
    }


def summary(from_time=None, to_time=None):
    """Return the full dashboard summary for the period.

    Assembles every section into one payload so an admin dashboard renders with
    a single request. When ``from``/``to`` are omitted the payload is all-time,
    and each section is computed over the same bounds so the numbers reconcile
    against the source tables.

    Args:
        from_time (datetime | None): inclusive lower bound, or None.
        to_time (datetime | None): inclusive upper bound, or None.

    Returns:
        dict: the assembled dashboard summary.
    """
    return {
        "period": {
            "from": from_time,
            "to": to_time,
        },
        "sales": sales_summary(from_time, to_time),
        "orders_by_status": orders_by_status(from_time, to_time),
        "orders_by_source": orders_by_source(from_time, to_time),
        "inquiries": inquiries_summary(from_time, to_time),
        "stock": stock_snapshot(),
        "catalogue": catalogue_summary(),
        "products": product_performance(from_time, to_time, limit=5),
        "traffic": traffic_summary(from_time, to_time),
        "promotions": promotions_summary(from_time, to_time),
        "returns": returns_summary(from_time, to_time),
        "support": support_summary(from_time, to_time),
        "reviews": reviews_summary(from_time, to_time),
        "notifications": notifications_summary(from_time, to_time),
    }
