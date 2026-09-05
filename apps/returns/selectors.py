"""Read-only query helpers for the returns app.

Selectors encapsulate query construction so views and serializers never build
raw querysets directly. They are pure reads with no side effects and use
``select_related``/``prefetch_related`` so list and detail endpoints never
trigger an N+1 query against the request's related rows.
"""

from apps.returns.models import ReturnRequest


def _base_queryset():
    """Return the shared queryset with all relations pre-fetched.

    Returns:
        QuerySet: return requests with their line, order, and audit trail
            read in as few queries as possible.
    """
    return (
        ReturnRequest.objects.select_related(
            "order",
            "order_item",
            "order_item__product",
            "order_item__fulfillment_warehouse",
        )
        .prefetch_related("status_history")
        .order_by("-created_at")
    )


def list_return_requests_for_order(order):
    """Return the return requests opened against an order.

    Args:
        order (Order): the order.

    Returns:
        QuerySet: the order's return requests, newest first.
    """
    return _base_queryset().filter(order=order)


def list_return_requests_for_user(user):
    """Return the return requests on the orders owned by a user.

    Args:
        user (User): the authenticated user.

    Returns:
        QuerySet: the user's return requests, newest first.
    """
    return _base_queryset().filter(order__user=user)


def get_return_request_for_order(order, return_request_id):
    """Return a single return request on an order, or None.

    Returns ``None`` rather than raising so the caller maps both a missing id
    and one belonging to another order to a 404.

    Args:
        order (Order): the order the request must belong to.
        return_request_id (int): the return request primary key.

    Returns:
        ReturnRequest | None: the matched request, or None.
    """
    return _base_queryset().filter(pk=return_request_id, order=order).first()


def list_all_return_requests():
    """Return every return request regardless of ownership.

    Used by staff-facing list endpoints that operate across all orders.

    Returns:
        QuerySet: all return requests, newest first.
    """
    return _base_queryset()


def get_return_request_for_staff(return_request_id):
    """Return any return request by id for a staff member, or None.

    Args:
        return_request_id (int): the return request primary key.

    Returns:
        ReturnRequest | None: the matched request, or None.
    """
    return _base_queryset().filter(pk=return_request_id).first()
