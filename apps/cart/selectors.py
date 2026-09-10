"""Read-only query helpers for the cart app.

Selectors encapsulate query construction so views and serializers never
build raw querysets directly.  They are pure reads with no side effects.
"""

from apps.cart.models import Cart, CartItem


def get_cart_for_user(user):
    """Return the active cart for an authenticated user, or None.

    Args:
        user (User): the authenticated user.

    Returns:
        Cart | None: the user's cart, or None if they have not started one.
    """
    return Cart.objects.filter(user=user).first()


def get_cart_for_session(session_key):
    """Return the active cart for a guest session, or None.

    Args:
        session_key (str): the guest session key.

    Returns:
        Cart | None: the guest's cart, or None if they have not started one.
    """
    if not session_key:
        return None
    return Cart.objects.filter(session_key=session_key).first()


def get_cart_items(cart):
    """Return cart items with their variant/bundle relations pre-fetched.

    Args:
        cart (Cart): the cart.

    Returns:
        QuerySet: cart items ordered by insertion order.
    """
    return (
        CartItem.objects.filter(cart=cart)
        .select_related(
            "variant__product",
            "bundle",
        )
        .order_by("pk")
    )
