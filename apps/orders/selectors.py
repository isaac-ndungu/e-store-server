"""Read-only query helpers for the orders app.

Selectors encapsulate query construction so views and serializers never build
raw querysets directly. They are pure reads with no side effects and use
``select_related``/``prefetch_related`` so staff endpoints never trigger an
N+1 query against the order's related rows.
"""

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
