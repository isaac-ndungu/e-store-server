"""Business logic for the cart app.

All mutation goes through service functions so the view layer stays thin.
Cart pricing recomputes effective prices from the promotions and bundles
services on every read — the stored ``price`` snapshot on ``CartItem`` is
never used for charging and exists solely for downstream order-history
reconstruction.

All money arithmetic uses ``Decimal`` throughout, matching the convention
of the promotions and bundles apps.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from apps.cart.models import Cart, CartItem, WishlistItem
from apps.cart.selectors import get_cart_items

logger = logging.getLogger(__name__)

_PENNY = Decimal("0.01")


def _money(value):
    """Return ``value`` as an exact ``Decimal``.

    ``DecimalField`` values are ``Decimal`` under PostgreSQL but plain
    strings under the in-memory SQLite used by tests, so any arithmetic on
    a money field must pass through here first.

    Args:
        value: a ``DecimalField`` value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def _touch_cart(cart):
    """Bump the cart's ``updated_at`` timestamp without other writes.

    Item and coupon mutations should register as cart activity so activity
    metrics (e.g. abandoned-cart reminders keyed on ``updated_at``) reflect
    the most recent change.

    Args:
        cart (Cart): the cart to touch.
    """
    cart.save(update_fields=["updated_at"])


#
# Cart retrieval / creation
#


def get_or_create_cart(user=None, session_key=None):
    """Return the active cart for a user or guest, creating one if needed.

    For authenticated users the lookup is by ``user``; for guests by
    ``session_key``.  An existing invalid coupon is silently removed so the
    cart stays in a valid state.

    Args:
        user (User | None): the authenticated user, or None for a guest.
        session_key (str | None): the guest session key, or None for an
            authenticated user.

    Returns:
        Cart: the user's or guest's active cart.
    """
    if user is not None and user.is_authenticated:
        cart, _created = Cart.objects.get_or_create(user=user, defaults={"user": user})
    elif session_key:
        cart, _created = Cart.objects.get_or_create(
            session_key=session_key, defaults={"session_key": session_key}
        )
    else:
        raise ValueError("Either user or session_key must be provided.")

    if cart.coupon_id is not None:
        _validate_cart_coupon(cart)
    return cart


def _validate_cart_coupon(cart):
    """Remove the coupon from a cart if it is no longer valid.

    Silent removal keeps the cart usable — the shopper sees an empty coupon
    field and can re-apply a valid code.

    Args:
        cart (Cart): the cart to validate.
    """
    from apps.promotions.services import validate_coupon

    coupon = cart.coupon
    if coupon is None:
        return
    user = cart.user if cart.user_id else None
    subtotal = _line_subtotal_sum(cart)
    result = validate_coupon(coupon, user=user, subtotal=subtotal)
    if not result["valid"]:
        logger.info(
            "Cart %s coupon %s no longer valid, removing: %s",
            cart.pk,
            coupon.code,
            result["reason"],
        )
        cart.coupon = None
        cart.save(update_fields=["coupon", "updated_at"])


def merge_guest_cart(user, session_key):
    """Adopt a guest cart into a user's authenticated cart.

    On login, the shopper expects the items they added as a guest to carry
    across.  Guest lines are merged into the user's cart, summing quantities
    for lines that reference the same variant or bundle; the guest coupon is
    carried over only when the user cart has none and it still validates
    against the merged cart.  The guest cart row is then removed so it cannot
    be addressed again.

    Args:
        user (User): the authenticated user adopting the cart.
        session_key (str | None): the guest session key, or None to skip.

    Returns:
        Cart: the user's cart after any merge.
    """
    if not session_key:
        user_cart, _created = Cart.objects.get_or_create(
            user=user, defaults={"user": user}
        )
        return user_cart

    guest_cart = Cart.objects.filter(session_key=session_key).first()
    if guest_cart is None:
        user_cart, _created = Cart.objects.get_or_create(
            user=user, defaults={"user": user}
        )
        return user_cart

    user_cart, _created = Cart.objects.get_or_create(user=user, defaults={"user": user})

    guest_items = list(guest_cart.items.all())
    for guest_item in guest_items:
        if guest_item.variant_id is not None:
            existing = CartItem.objects.filter(
                cart=user_cart, variant_id=guest_item.variant_id
            ).first()
        elif guest_item.bundle_id is not None:
            existing = CartItem.objects.filter(
                cart=user_cart, bundle_id=guest_item.bundle_id
            ).first()
        else:
            continue

        if existing:
            existing.quantity = existing.quantity + guest_item.quantity
            existing.save(update_fields=["quantity", "added_at"])
        else:
            CartItem.objects.create(
                cart=user_cart,
                variant_id=guest_item.variant_id,
                bundle_id=guest_item.bundle_id,
                quantity=guest_item.quantity,
            )
    if guest_items:
        _touch_cart(user_cart)

    if user_cart.coupon_id is None and guest_cart.coupon_id is not None:
        user_cart.coupon = guest_cart.coupon
        user_cart.save(update_fields=["coupon", "updated_at"])
        _validate_cart_coupon(user_cart)

    guest_cart.delete()
    return user_cart


# Cart item management


def add_item(cart, *, variant_id=None, bundle_id=None, quantity=1):
    """Add a variant or bundle to the cart, or increment an existing line.

    Validates that the target exists and is active, checks stock for
    variants, and merges duplicate lines by summing the quantity.

    Args:
        cart (Cart): the target cart.
        variant_id (int | None): the product variant id.
        bundle_id (int | None): the bundle id.
        quantity (int): how many units to add (default 1).

    Returns:
        CartItem: the created or updated cart item.

    Raises:
        ValidationError: if the item is invalid, inactive, or out of stock.
    """
    if quantity < 1:
        raise ValidationError("Quantity must be at least 1.")
    if not variant_id and not bundle_id:
        raise ValidationError("Either variant_id or bundle_id is required.")
    if variant_id and bundle_id:
        raise ValidationError("Provide either variant_id or bundle_id, not both.")

    if variant_id:
        _validate_variant(variant_id, quantity)
        _check_stock(variant_id, quantity)
        existing = CartItem.objects.filter(cart=cart, variant_id=variant_id).first()
    else:
        _validate_bundle(bundle_id)
        existing = CartItem.objects.filter(cart=cart, bundle_id=bundle_id).first()

    if existing:
        new_qty = existing.quantity + quantity
        if variant_id:
            _check_stock(variant_id, new_qty)
        existing.quantity = new_qty
        existing.save(update_fields=["quantity", "added_at"])
        _touch_cart(cart)
        return existing

    cart_item = CartItem.objects.create(
        cart=cart,
        variant_id=variant_id,
        bundle_id=bundle_id,
        quantity=quantity,
    )
    _touch_cart(cart)
    return cart_item


def update_item_quantity(cart, item_id, quantity):
    """Set the quantity of a cart item.

    Setting the quantity to 0 or negative removes the item instead.

    Args:
        cart (Cart): the owning cart.
        item_id (int): the cart-item id.
        quantity (int): the new quantity (0 or less removes the item).

    Returns:
        CartItem | None: the updated item, or None if removed.

    Raises:
        ValidationError: if the item is not found or stock is insufficient.
    """
    item = _get_owned_item(cart, item_id)
    if quantity <= 0:
        item.delete()
        _touch_cart(cart)
        return None

    if item.variant_id:
        _check_stock(item.variant_id, quantity)

    item.quantity = quantity
    item.save(update_fields=["quantity", "added_at"])
    _touch_cart(cart)
    return item


def remove_item(cart, item_id):
    """Remove a cart item entirely.

    Args:
        cart (Cart): the owning cart.
        item_id (int): the cart-item id.

    Raises:
        ValidationError: if the item is not found.
    """
    item = _get_owned_item(cart, item_id)
    item.delete()
    _touch_cart(cart)


# ---------------------------------------------------------------------------
# Coupon management
# ---------------------------------------------------------------------------


def apply_coupon(cart, code, user=None):
    """Validate and apply a coupon code to the cart.

    The coupon is validated against intrinsic rules (active, in window,
    usage limits, minimum order value) and the cart's subtotal before being
    attached.

    Args:
        cart (Cart): the target cart.
        code (str): the coupon code.
        user (User | None): the applying user, for per-user limits.

    Returns:
        dict: the validation result from ``validate_coupon_code``.

    Raises:
        ValidationError: if the coupon is invalid.
    """
    from apps.promotions.services import validate_coupon_code

    subtotal = _line_subtotal_sum(cart)
    result = validate_coupon_code(code, user=user, subtotal=subtotal)
    if not result["valid"]:
        raise ValidationError(result.get("reason", "Invalid coupon."))

    from apps.promotions.models import Coupon

    coupon = Coupon.objects.get(code__iexact=code.strip())
    cart.coupon = coupon
    cart.save(update_fields=["coupon", "updated_at"])
    return result


def remove_coupon(cart):
    """Remove the coupon from the cart.

    Args:
        cart (Cart): the target cart.
    """
    if cart.coupon_id is not None:
        cart.coupon = None
        cart.save(update_fields=["coupon", "updated_at"])


# Cart totals computation


def compute_cart_totals(cart):
    """Compute the full price breakdown for a cart.

    Every line is priced server-side through the promotions / bundles
    services.  Money fields are strings in the result to travel exactly as
    computed.  The coupon adjustment (if any) is applied at the line level,
    per the coupon's product/category restrictions and stacking rules.

    ``subtotal`` is the net cost of the goods — the sum of the per-line
    prices actually charged after both automatic discounts and the coupon.
    ``discount_total`` and ``coupon_discount`` are informational savings
    breakdowns; they are not subtracted from ``subtotal`` a second time.
    ``total`` is ``subtotal`` plus ``vat_total`` — what the shopper pays for
    the goods including tax, excluding shipping.

    The ``vat_breakdown`` dict keys are ``standard``, ``zero_rated``, and
    ``exempt``, with amounts as decimal strings.  Tax is computed from each
    line's product ``tax_class`` and the ``SiteConfig.standard_vat_rate``
    — never from a single flat rate, since a cart can mix products with
    different tax treatments.

    Args:
        cart (Cart): the cart to price.

    Returns:
        dict: ``items``, ``item_count``, ``subtotal``, ``discount_total``,
            ``coupon_code``, ``coupon_discount``, ``vat_breakdown``,
            ``vat_total``, and ``total`` — money as decimal strings.
    """
    items = list(get_cart_items(cart))

    coupon = cart.coupon
    line_items = []
    subtotal = Decimal("0.00")
    discount_total = Decimal("0.00")
    vat_breakdown = {
        "standard": Decimal("0.00"),
        "zero_rated": Decimal("0.00"),
        "exempt": Decimal("0.00"),
    }

    for item in items:
        if item.variant_id is not None:
            line = _price_variant_item(item, coupon)
        else:
            line = _price_bundle_item(item, coupon)

        subtotal += line["line_subtotal"]
        discount_total += line["line_discount"]

        tax_class = _get_tax_class(item)
        tax_rate = _get_tax_rate(tax_class)
        line["tax_class"] = tax_class
        line["tax_rate"] = str(tax_rate)
        line["tax"] = str(
            (line["line_subtotal"] * tax_rate / Decimal("100")).quantize(
                _PENNY, rounding=ROUND_HALF_UP
            )
        )
        vat_breakdown[tax_class] += (
            line["line_subtotal"] * tax_rate / Decimal("100")
        ).quantize(_PENNY, rounding=ROUND_HALF_UP)

        line_items.append(line)

    coupon_discount = Decimal("0.00")
    coupon_code = None
    if coupon is not None:
        coupon_discount = min(
            sum(li["coupon_savings"] for li in line_items), subtotal
        ).quantize(_PENNY, rounding=ROUND_HALF_UP)
        coupon_code = coupon.code

    vat_total = sum(vat_breakdown.values(), Decimal("0.00"))
    total = max(subtotal + vat_total, Decimal("0.00"))

    return {
        "items": line_items,
        "item_count": sum(li["quantity"] for li in line_items),
        "subtotal": str(subtotal.quantize(_PENNY, rounding=ROUND_HALF_UP)),
        "discount_total": str(discount_total.quantize(_PENNY, rounding=ROUND_HALF_UP)),
        "coupon_code": coupon_code,
        "coupon_discount": str(
            coupon_discount.quantize(_PENNY, rounding=ROUND_HALF_UP)
        ),
        "vat_breakdown": {
            k: str(v.quantize(_PENNY, rounding=ROUND_HALF_UP))
            for k, v in vat_breakdown.items()
        },
        "vat_total": str(vat_total.quantize(_PENNY, rounding=ROUND_HALF_UP)),
        "total": str(total.quantize(_PENNY, rounding=ROUND_HALF_UP)),
    }


def _price_variant_item(item, coupon):
    """Price a variant cart line through the promotions service.

    Args:
        item (CartItem): the cart item (must have ``variant_id``).
        coupon (Coupon | None): the active cart coupon.

    Returns:
        dict: the priced line item detail.
    """
    from apps.promotions.services import get_effective_price

    variant = item.variant
    price_data = get_effective_price(variant, coupon=coupon)
    unit_price = Decimal(price_data["price"])
    base_price = Decimal(price_data["base_price"])
    line_subtotal = (unit_price * item.quantity).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )
    coupon_savings = (Decimal(price_data["coupon_discount"]) * item.quantity).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )
    price_discount = Decimal(price_data["discount"]) * item.quantity
    auto_discount = (price_discount - coupon_savings).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )

    stock_info = _get_stock_info(variant)

    return {
        "item_id": item.pk,
        "type": "variant",
        "variant_id": variant.pk,
        "product_name": variant.product.name,
        "variant_attributes": variant.attributes,
        "sku": variant.sku,
        "unit_price": str(unit_price),
        "base_price": str(base_price),
        "quantity": item.quantity,
        "line_subtotal": line_subtotal,
        "line_discount": auto_discount,
        "coupon_savings": coupon_savings,
        "stock_available": stock_info["available"],
        "in_stock": stock_info["available"] >= item.quantity,
    }


def _price_bundle_item(item, coupon):
    """Price a bundle cart line through the bundles service.

    The bundle price is the discounted price for the whole set.  No coupon
    adjustment is applied at this level — bundle pricing is self-contained
    and a coupon discount is evaluated against the cart subtotal.

    Args:
        item (CartItem): the cart item (must have ``bundle_id``).
        coupon (Coupon | None): the active cart coupon (unused here).

    Returns:
        dict: the priced line item detail.
    """
    from apps.bundles.services import get_bundle_price

    bundle = item.bundle
    price_data = get_bundle_price(bundle)
    unit_price = Decimal(price_data["price"])
    regular_price = Decimal(price_data["regular_price"])
    line_subtotal = (unit_price * item.quantity).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )
    bundle_discount = (Decimal(price_data["discount"]) * item.quantity).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )

    return {
        "item_id": item.pk,
        "type": "bundle",
        "bundle_id": bundle.pk,
        "bundle_name": bundle.name,
        "sku": None,
        "unit_price": str(unit_price),
        "base_price": str(regular_price),
        "quantity": item.quantity,
        "line_subtotal": line_subtotal,
        "line_discount": bundle_discount,
        "coupon_savings": Decimal("0.00"),
        "stock_available": None,
        "in_stock": True,
    }


def _get_stock_info(variant):
    """Return aggregate stock availability for a variant across warehouses.

    Args:
        variant (ProductVariant): the variant to check.

    Returns:
        dict: ``available`` (int) and ``total`` (int) stock counts.
    """
    from apps.inventory.models import Inventory

    agg = Inventory.objects.filter(variant=variant).aggregate(
        total=Sum("quantity"),
        reserved=Sum("reserved"),
    )
    total = agg["total"] or 0
    reserved = agg["reserved"] or 0
    return {"available": max(total - reserved, 0), "total": total}


def _get_tax_class(item):
    """Return the tax class for a cart item.

    For variant items the tax class comes from the product.  For bundle
    items it is derived from the bundle's included (non-optional) component
    products: it is ``zero_rated`` or ``exempt`` only when every component
    shares that treatment, otherwise ``standard``.  This is a display-level
    preview — the authoritative per-component tax is computed when the
    bundle decomposes into individual order lines.

    Args:
        item (CartItem): the cart item.

    Returns:
        str: ``standard``, ``zero_rated``, or ``exempt``.
    """
    if item.variant_id is not None:
        return item.variant.product.tax_class
    return _bundle_tax_class(item.bundle)


def _bundle_tax_class(bundle):
    """Return the conservative tax class for a bundle's components.

    A bundle is ``zero_rated`` or ``exempt`` only when all of its included
    (non-optional) component products are, so a single standard-rated
    component keeps the whole bundle standard-rated.

    Args:
        bundle (Bundle): the bundle.

    Returns:
        str: ``standard``, ``zero_rated``, or ``exempt``.
    """
    component_classes = {
        bi.product.tax_class
        for bi in bundle.items.select_related("product").filter(is_optional=False)
    }
    if len(component_classes) == 1:
        return next(iter(component_classes))
    return "standard"


def _get_tax_rate(tax_class):
    """Return the VAT rate for a tax class.

    Uses ``SiteConfig.standard_vat_rate`` for standard-rated items and
    ``0`` for zero-rated and exempt items.

    Args:
        tax_class (str): the tax class key.

    Returns:
        Decimal: the VAT rate as a percentage (e.g. 16.00 for 16%).
    """
    if tax_class == "standard":
        from apps.core.models import SiteConfig

        return _money(SiteConfig.load().standard_vat_rate)
    return Decimal("0.00")


def _line_subtotal_sum(cart):
    """Return the sum of all line subtotals (before coupon) for a cart.

    Used to check a coupon's minimum order value at apply time.

    Args:
        cart (Cart): the cart.

    Returns:
        Decimal: the sum of all line subtotals.
    """
    total = Decimal("0.00")
    for item in cart.items.select_related("variant__product", "bundle").order_by("pk"):
        if item.variant_id is not None:
            from apps.promotions.services import get_effective_price

            price_data = get_effective_price(item.variant)
            unit_price = Decimal(price_data["price"])
        else:
            from apps.bundles.services import get_bundle_price

            price_data = get_bundle_price(item.bundle)
            unit_price = Decimal(price_data["price"])
        total += unit_price * item.quantity
    return total


# Validation helpers


def _validate_variant(variant_id, quantity):
    """Reject an inactive or missing variant.

    Args:
        variant_id (int): the variant id.
        quantity (int): the requested quantity.

    Raises:
        ValidationError: if the variant is missing or inactive.
    """
    from apps.catalog.models import ProductVariant

    variant = ProductVariant.objects.filter(pk=variant_id).first()
    if variant is None:
        raise ValidationError("No such product variant.")
    if not variant.is_active:
        raise ValidationError("This product variant is no longer available.")


def _validate_bundle(bundle_id):
    """Reject an inactive or missing bundle.

    Args:
        bundle_id (int): the bundle id.

    Raises:
        ValidationError: if the bundle is missing or inactive.
    """
    from apps.bundles.models import Bundle

    bundle = Bundle.objects.filter(pk=bundle_id).first()
    if bundle is None:
        raise ValidationError("No such bundle.")
    if not bundle.is_active:
        raise ValidationError("This bundle is no longer available.")


def _check_stock(variant_id, quantity):
    """Reject a quantity that exceeds available stock.

    Args:
        variant_id (int): the variant id.
        quantity (int): the total quantity requested.

    Raises:
        ValidationError: if stock is insufficient.
    """
    from apps.catalog.models import ProductVariant

    variant = ProductVariant.objects.filter(pk=variant_id).first()
    stock = _get_stock_info(variant)
    if stock["available"] < quantity:
        raise ValidationError(
            f"Insufficient stock. {stock['available']} units available."
        )


def _get_owned_item(cart, item_id):
    """Return a cart item that belongs to the given cart, or raise.

    Args:
        cart (Cart): the owning cart.
        item_id (int): the cart-item id.

    Returns:
        CartItem: the cart item.

    Raises:
        ValidationError: if the item does not exist or does not belong to
            the cart.
    """
    item = CartItem.objects.filter(pk=item_id, cart=cart).first()
    if item is None:
        raise ValidationError("Cart item not found.")
    return item


# Wishlist services


def add_to_wishlist(user, product_id):
    """Add a product to the user's wishlist.

    If the product is already in the wishlist the operation is a no-op and
    the existing item is returned.

    Args:
        user (User): the authenticated user.
        product_id (int): the product id.

    Returns:
        WishlistItem: the wishlist item.

    Raises:
        ValidationError: if the product does not exist.
    """
    from apps.catalog.models import Product

    product = Product.objects.filter(pk=product_id).first()
    if product is None:
        raise ValidationError("No such product.")
    item, _created = WishlistItem.objects.get_or_create(user=user, product=product)
    return item


def remove_from_wishlist(user, product_id):
    """Remove a product from the user's wishlist.

    Args:
        user (User): the authenticated user.
        product_id (int): the product id.

    Raises:
        ValidationError: if the wishlist item does not exist.
    """
    deleted, _count = WishlistItem.objects.filter(
        user=user, product_id=product_id
    ).delete()
    if deleted == 0:
        raise ValidationError("Product is not in your wishlist.")


def list_wishlist(user):
    """Return all products in the user's wishlist.

    Args:
        user (User): the authenticated user.

    Returns:
        QuerySet: wishlist items with products pre-fetched.
    """
    return WishlistItem.objects.filter(user=user).select_related("product")
