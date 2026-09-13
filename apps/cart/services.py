"""Business logic for the cart app.

Mutations are scoped to the visitor's own cart (resolved server-side from
the cookie), so a caller can only ever touch lines they already possess the
cookie for. Every line is validated against live catalogue state — an
inactive or staff-marked-out-of-stock variant cannot be added — and prices
are never accepted from the caller.
"""

from django.core.exceptions import ValidationError

from apps.bundles.models import Bundle
from apps.cart.models import CartItem
from apps.cart.selectors import MAX_LINE_QUANTITY, variant_is_orderable
from apps.catalog.models import ProductVariant


def _get_orderable_variant(variant_id):
    """Return the active variant for a cart line or raise.

    Args:
        variant_id (int): the catalogue variant pk.

    Returns:
        ProductVariant: the variant with its product prefetched.

    Raises:
        ValidationError: when the variant is unknown, inactive, or marked
            out of stock.
    """
    try:
        variant = ProductVariant.objects.select_related("product").get(pk=variant_id)
    except ProductVariant.DoesNotExist, ValueError, TypeError:
        raise ValidationError(f"Variant {variant_id} is not available.") from None
    if not variant_is_orderable(variant):
        raise ValidationError(f"Variant {variant.sku} is not available.")
    return variant


def add_item(cart, *, variant_id, quantity=1, bundle_id=None):
    """Add a line to the cart, merging with an identical existing line.

    Args:
        cart (Cart): the visitor's cart.
        variant_id (int): the catalogue variant to add.
        quantity (int): units to add (1-999).
        bundle_id (int | None): optional bundle the line belongs to.

    Returns:
        CartItem: the created or merged line.

    Raises:
        ValidationError: on bad quantity, unknown variant or bundle, or an
            unavailable variant.
    """
    try:
        quantity = int(quantity)
    except TypeError, ValueError:
        raise ValidationError("Quantity must be a whole number.") from None
    if quantity < 1 or quantity > MAX_LINE_QUANTITY:
        raise ValidationError(f"Quantity must be between 1 and {MAX_LINE_QUANTITY}.")
    variant = _get_orderable_variant(variant_id)
    bundle = None
    if bundle_id is not None:
        try:
            bundle = Bundle.objects.get(pk=bundle_id)
        except Bundle.DoesNotExist, ValueError, TypeError:
            raise ValidationError(f"Bundle {bundle_id} is not available.") from None
    existing = (
        CartItem.objects.filter(cart=cart, variant=variant, bundle=bundle)
        .order_by("pk")
        .first()
    )
    if existing is not None:
        existing.quantity = min(existing.quantity + quantity, MAX_LINE_QUANTITY)
        existing.save(update_fields=["quantity"])
        cart.save(update_fields=["updated_at"])
        return existing
    item = CartItem.objects.create(
        cart=cart, variant=variant, bundle=bundle, quantity=quantity
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
        item = CartItem.objects.select_related("variant__product").get(
            pk=item_id, cart=cart
        )
    except CartItem.DoesNotExist, ValueError, TypeError:
        raise ValidationError("Cart item not found.") from None
    if not variant_is_orderable(item.variant):
        raise ValidationError(f"Variant {item.variant.sku} is no longer available.")
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
