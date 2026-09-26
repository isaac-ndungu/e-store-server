"""Business logic for the cart app.

Mutations are scoped to the visitor's own cart (resolved server-side from
the cookie), so a caller can only ever touch lines they already possess the
cookie for. Every line is validated against live catalogue state  -  an
inactive or staff-marked-out-of-stock product cannot be added  -  and prices
are never accepted from the caller.
"""

from django.core.exceptions import ValidationError

from apps.bundles.models import Bundle
from apps.cart.models import CartItem
from apps.cart.selectors import MAX_LINE_QUANTITY, product_is_orderable
from apps.catalog.models import Product


def _get_orderable_product(product_id):
    """Return the active product for a cart line or raise.

    Args:
        product_id (int): the catalogue product pk.

    Returns:
        Product: the orderable product.

    Raises:
        ValidationError: when the product is unknown, inactive, or marked
            out of stock.
    """
    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist, ValueError, TypeError:
        raise ValidationError(f"Product {product_id} is not available.") from None
    if not product_is_orderable(product):
        raise ValidationError(f"Product {product.sku} is not available.")
    return product


def add_item(cart, *, product_id, quantity=1, bundle_id=None):
    """Add a line to the cart, merging with an identical existing line.

    Args:
        cart (Cart): the visitor's cart.
        product_id (int): the catalogue product to add.
        quantity (int): units to add (1-999).
        bundle_id (int | None): optional bundle the line belongs to.

    Returns:
        CartItem: the created or merged line.

    Raises:
        ValidationError: on bad quantity, unknown product or bundle, or an
            unavailable product.
    """
    try:
        quantity = int(quantity)
    except TypeError, ValueError:
        raise ValidationError("Quantity must be a whole number.") from None
    if quantity < 1 or quantity > MAX_LINE_QUANTITY:
        raise ValidationError(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}.")
    product = _get_orderable_product(product_id)
    bundle = None
    if bundle_id is not None:
        try:
            bundle = Bundle.objects.get(pk=bundle_id)
        except Bundle.DoesNotExist, ValueError, TypeError:
            raise ValidationError(f"Bundle {bundle_id} is not available.") from None
    existing = (
        CartItem.objects.filter(cart=cart, product=product, bundle=bundle)
        .order_by("pk")
        .first()
    )
    if existing is not None:
        existing.quantity = min(existing.quantity + quantity, MAX_LINE_QUANTITY)
        existing.save(update_fields=["quantity"])
        cart.save(update_fields=["updated_at"])
        return existing
    item = CartItem.objects.create(
        cart=cart, product=product, bundle=bundle, quantity=quantity
    )
    cart.save(update_fields=["updated_at"])
    return item


def update_item(cart, item_id, *, quantity):
    """Set a cart line's quantity.

    Args:
        cart (Cart): the visitor's cart.
        item_id (int): the line pk, must belong to ``cart``.
        quantity (int): the new quantity (1-999).

    Returns:
        CartItem: the updated line.

    Raises:
        ValidationError: on bad quantity or a line outside this cart.
    """
    try:
        quantity = int(quantity)
    except TypeError, ValueError:
        raise ValidationError("Quantity must be a whole number.") from None
    if quantity < 1 or quantity > MAX_LINE_QUANTITY:
        raise ValidationError(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}.")
    try:
        item = CartItem.objects.select_related("product").get(pk=item_id, cart=cart)
    except CartItem.DoesNotExist, ValueError, TypeError:
        raise ValidationError("Cart item not found.") from None
    if not product_is_orderable(item.product):
        raise ValidationError(f"Product {item.product.sku} is no longer available.")
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    cart.save(update_fields=["updated_at"])
    return item


def remove_item(cart, item_id):
    """Remove a line from the cart.

    Args:
        cart (Cart): the visitor's cart.
        item_id (int): the line pk, must belong to ``cart``.

    Raises:
        ValidationError: when the line is outside this cart.
    """
    try:
        item = CartItem.objects.get(pk=item_id, cart=cart)
    except CartItem.DoesNotExist, ValueError, TypeError:
        raise ValidationError("Cart item not found.") from None
    item.delete()
    cart.save(update_fields=["updated_at"])
