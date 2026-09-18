"""Read-only query helpers for the orders app.

Selectors encapsulate query construction so views and serializers never build
raw querysets directly. They are pure reads with no side effects and use
``select_related``/``prefetch_related`` so staff endpoints never trigger an
N+1 query against the order's related rows.
"""

from django.db.models import Count

from apps.orders.models import Order


def find_orders_by_payment_reference(reference, exclude_pk=None):
    """Return orders already carrying a staff-entered payment reference.

    A receipt code fat-fingered onto two orders is a reconciliation headache,
    so the intake view warns when the reference being filed already appears
    elsewhere. Blank references are skipped — most non-M-Pesa orders carry
    none, and warning on those would be pure noise.

    Args:
        reference (str): the payment reference just filed.
        exclude_pk (int | None): order pk to leave out (the row just created).

    Returns:
        list: up to five ``{"id", "lookup_token"}`` dicts, oldest first.
    """
    reference = (reference or "").strip()
    if not reference:
        return []
    queryset = Order.objects.filter(payment_reference=reference).order_by(
        "placed_at", "pk"
    )
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return [
        {"id": order.pk, "lookup_token": str(order.lookup_token)}
        for order in queryset.only("lookup_token")[:5]
    ]


def get_order_for_staff(order_id):
    """Return any order by id for a staff member, or None.

    Used by staff views that operate across all orders. A missing id maps to
    a 404 by the caller.

    Args:
        order_id (int): the order primary key.

    Returns:
        Order | None: the order with items and status history pre-fetched, or
            None when no order matches the id.
    """
    return (
        Order.objects.filter(pk=order_id)
        .prefetch_related("items", "status_history")
        .select_related("delivery_area", "shipping_address")
        .first()
    )


def list_staff_orders(status=None, phone=None, order_source=None,
                      placed_from=None, placed_to=None):
    """Return the staff order queue newest-first with per-row item counts.

    All filters are optional and combine with AND. The item count is annotated
    so the list serializer never issues a query per row.

    Args:
        status (str | None): exact order status to keep.
        phone (str | None): case-insensitive substring of the order phone.
        order_source (str | None): exact order source to keep.
        placed_from: keep orders placed at or after this datetime.
        placed_to: keep orders placed at or before this datetime.

    Returns:
        QuerySet: annotated orders ordered newest-first.
    """
    queryset = Order.objects.all().order_by("-placed_at", "-pk")
    if status:
        queryset = queryset.filter(status=status)
    if phone:
        queryset = queryset.filter(phone__icontains=phone)
    if order_source:
        queryset = queryset.filter(order_source=order_source)
    if placed_from is not None:
        queryset = queryset.filter(placed_at__gte=placed_from)
    if placed_to is not None:
        queryset = queryset.filter(placed_at__lte=placed_to)
    return queryset.annotate(item_count=Count("items"))


def get_order_items(order):
    """Return an order's line items with relations pre-fetched.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the order's items ordered by insertion order.
    """
    return order.items.select_related("product", "bundle")


def get_order_status_history(order):
    """Return an order's status audit trail.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the order's status history ordered by time.
    """
    return order.status_history.select_related("changed_by")
