"""Read-only query helpers for the orders app.

Selectors encapsulate query construction so views and serializers never build
raw querysets directly. They are pure reads with no side effects and use
``select_related``/``prefetch_related`` so list and detail endpoints never
trigger an N+1 query against the order's related rows.
"""

from apps.orders.models import Order


def list_orders_for_user(user):
    """Return the orders belonging to a user.

    Guests have no user row, so only authenticated users can list orders
    through this path. A guest order is addressed by its id via the detail
    selector, which additionally requires the ``phone`` to match.

    Args:
        user (User): the authenticated user.

    Returns:
        QuerySet: the user's orders ordered newest-first with items and
            status history pre-fetched.
    """
    return (
        Order.objects.filter(user=user)
        .prefetch_related("items", "status_history")
        .order_by("-placed_at")
    )


def get_order_for_user(user, order_id):
    """Return a single order owned by the user, or None.

    Returns ``None`` for an order belonging to another user or for a missing
    id so the caller can map both to a 404 without leaking which ids exist.

    Args:
        user (User): the authenticated user.
        order_id (int): the order primary key.

    Returns:
        Order | None: the order with items and status history pre-fetched, or
            None when absent or not owned by the user.
    """
    return (
        Order.objects.filter(pk=order_id, user=user)
        .prefetch_related("items", "status_history", "verification")
        .select_related("delivery_zone", "shipping_address")
        .first()
    )


def get_order_by_token(token):
    """Return a single order matching an unguessable lookup token, or None.

    Used to resolve a guest order for viewing, OTP verification, and
    cancellation, where the order has no owning user. The token is a random
    UUID returned to the caller at order creation, so a guest can address
    their order without a login and without another caller being able to
    enumerate orders by guessing sequential ids or a phone number.

    Args:
        token (str | UUID): the order's lookup token.

    Returns:
        Order | None: the order with items and status history pre-fetched, or
            None when no order matches the token.
    """
    return (
        Order.objects.filter(lookup_token=token)
        .prefetch_related("items", "status_history", "verification")
        .select_related("delivery_zone", "shipping_address")
        .first()
    )


def get_order_for_staff(order_id):
    """Return any order by id for a staff member, or None.

    Used by admin/courier-facing views that operate across all orders rather
    than only the caller's own. A missing id maps to a 404 by the caller.

    Args:
        order_id (int): the order primary key.

    Returns:
        Order | None: the order with items and status history pre-fetched, or
            None when no order matches the id.
    """
    return (
        Order.objects.filter(pk=order_id)
        .prefetch_related("items", "status_history", "verification")
        .select_related("delivery_zone", "shipping_address")
        .first()
    )


def get_order_items(order):
    """Return an order's line items with relations pre-fetched.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the order's items ordered by insertion order.
    """
    return order.items.select_related("product", "bundle", "fulfillment_warehouse")


def get_order_status_history(order):
    """Return an order's status audit trail.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the order's status history ordered by time.
    """
    return order.status_history.select_related("changed_by")


def get_active_verification(order):
    """Return an order's verification record, or None.

    Args:
        order (Order): the order.

    Returns:
        OrderVerification | None: the order's verification, or None if the
            order has none.
    """
    try:
        return order.verification
    except Order.verification.RelatedObjectDoesNotExist:
        return None
