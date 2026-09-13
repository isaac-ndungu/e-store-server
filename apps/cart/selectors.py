"""Read helpers for the cart app.

Selectors resolve the visitor's cart from the request cookie and shape the
cart read model with server-computed prices. Nothing here writes to the
database except the get-or-create of an empty cart for a new visitor.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.cart.models import Cart

CART_COOKIE_NAME = "estore_cart"
CART_COOKIE_MAX_AGE = 90 * 24 * 60 * 60

MAX_LINE_QUANTITY = 999


def variant_is_orderable(variant):
    """Return whether a variant may be added to a cart.

    Args:
        variant (ProductVariant): the variant with ``product`` prefetched.

    Returns:
        bool: False when the variant or its product is inactive, or staff
            have marked the variant out of stock.
    """
    if not variant.is_active:
        return False
    product = getattr(variant, "product", None)
    if product is not None and not product.is_active:
        return False
    return variant.stock_status != "out_of_stock"


def get_cart_from_cookie(cookies):
    """Return the visitor's cart for the request cookies, if any.

    Args:
        cookies (dict): the request's cookie mapping.

    Returns:
        Cart | None: the matching cart, or None when the cookie is
            missing, malformed, or points at no cart.
    """
    raw = cookies.get(CART_COOKIE_NAME)
    if not raw:
        return None
    try:
        return Cart.objects.prefetch_related(
            "items__variant__product", "items__bundle"
        ).get(anonymous_id=raw)
    except Cart.DoesNotExist, ValidationError, ValueError, TypeError:
        return None


def get_or_create_cart(cookies):
    """Return the visitor's cart, creating an empty one when needed.

    Args:
        cookies (dict): the request's cookie mapping.

    Returns:
        tuple: ``(cart, created)`` with the resolved cart and whether it
            was just created.
    """
    cart = get_cart_from_cookie(cookies)
    if cart is not None:
        return cart, False
    return Cart.objects.create(), True


def price_cart_lines(cart):
    """Price every line of a cart server-side.

    Args:
        cart (Cart): the cart with items prefetched.

    Returns:
        tuple: ``(lines, subtotal)`` where lines is a list of dicts with
            the item, variant, bundle, quantity, unit price, and line
            total, and subtotal is the Decimal sum of line totals.
    """
    from apps.promotions.services import get_effective_price

    lines = []
    subtotal = Decimal("0.00")
    for item in cart.items.all():
        effective = get_effective_price(
            item.variant, within_bundle=item.bundle_id is not None
        )
        unit_price = Decimal(effective["price"])
        line_total = unit_price * item.quantity
        subtotal += line_total
        lines.append(
            {
                "item": item,
                "variant": item.variant,
                "bundle": item.bundle,
                "quantity": item.quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )
    return lines, subtotal


def build_cart_snapshot(cart):
    """Build the staff-facing snapshot lines for a cart.

    Used when an inquiry is captured: staff see sku, name, quantity, and
    the display-time price for each line. Prices stay display data — intake
    reprices everything.

    Args:
        cart (Cart): the cart with items prefetched.

    Returns:
        list: snapshot dicts with ``sku``, ``name``, ``quantity``, and
            ``price`` (string).
    """
    priced_lines, _ = price_cart_lines(cart)
    snapshot = []
    for entry in priced_lines:
        variant = entry["variant"]
        product = getattr(variant, "product", None)
        snapshot.append(
            {
                "sku": variant.sku,
                "name": product.name if product is not None else variant.sku,
                "quantity": entry["quantity"],
                "price": str(entry["unit_price"]),
            }
        )
    return snapshot
