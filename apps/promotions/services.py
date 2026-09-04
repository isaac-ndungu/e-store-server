import logging
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.promotions import cache
from apps.promotions.models import Coupon, CouponRedemption, Discount

logger = logging.getLogger(__name__)

# Base used by percent discounts to keep the math in exact decimal form.
_PERCENT_BASE = Decimal("100")
_PENNY = Decimal("0.01")


def _money(value):
    """Return ``value`` as an exact ``Decimal``.

    ``DecimalField`` values are ``Decimal`` under PostgreSQL but plain strings
    under the in-memory SQLite used by tests, so any arithmetic on a money
    field must pass through here first.

    Args:
        value: a ``DecimalField`` value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def _in_window(start, end):
    """Return whether now falls inside the given window.

    Args:
        start (datetime): the window start.
        end (datetime | None): the window end, or None for open-ended.

    Returns:
        bool: True when now is inside the window.
    """
    now = timezone.now()
    if start and start > now:
        return False
    if end and end < now:
        return False
    return True


def _discounts_for_variant(variant, within_bundle):
    """Return applicable, currently active discounts for a variant.

    Filters active, in-window discounts with a redemption budget remaining,
    matched to the variant by the discount's scope. When ``within_bundle`` is
    True only discounts with ``applies_within_bundles`` set are returned, so
    individual-item promotions do not stack with a bundle's own discount.

    Args:
        variant (ProductVariant): the variant to price.
        within_bundle (bool): whether the price is inside a bundle.

    Returns:
        list[Discount]: the applicable discounts.
    """
    now = timezone.now()
    queryset = (
        Discount.objects.filter(is_active=True, starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
        .select_related("bundle")
    )
    if within_bundle:
        queryset = queryset.filter(applies_within_bundles=True)

    applicable = []
    for discount in queryset:
        if discount.max_redemptions is not None and (
            discount.redemption_count >= discount.max_redemptions
        ):
            continue
        if _discount_matches(discount, variant):
            applicable.append(discount)
    return applicable


def _discount_matches(discount, variant):
    """Return whether a discount's scope covers the variant.

    A bundle-scoped discount applies to the whole bundle's price, not to an
    individual variant lookup, so it is never returned here.

    Args:
        discount (Discount): the discount to evaluate.
        variant (ProductVariant): the variant being priced.

    Returns:
        bool: True when the discount applies to the variant.
    """
    scope = discount.scope
    if scope == "bundle":
        return False
    if scope == "sitewide":
        return True
    if scope == "variant":
        return discount.variants.filter(pk=variant.pk).exists()
    if scope == "product":
        return discount.products.filter(pk=variant.product_id).exists()
    if scope == "category":
        category_id = variant.product.category_id
        return (
            category_id is not None
            and discount.categories.filter(pk=category_id).exists()
        )
    if scope == "brand":
        brand_id = variant.product.brand_id
        return brand_id is not None and discount.brands.filter(pk=brand_id).exists()
    return False


def _apply_discount(price, discount):
    """Return the price after applying a discount, floored at zero.

    Args:
        price (Decimal): the price to discount.
        discount (Discount): the discount to apply.

    Returns:
        Decimal: the discounted price.
    """
    value = _money(discount.value)
    if discount.discount_type == "percent":
        amount = (price * value) / _PERCENT_BASE
    else:
        amount = value
    discounted = max(price - amount, Decimal("0.00"))
    return discounted.quantize(_PENNY, rounding=ROUND_HALF_UP)


def _best_discount(variant, within_bundle):
    """Return the best discount for a variant, or None.

    Chooses the highest-priority applicable discount; ties go to the one
    yielding the lowest price.

    Args:
        variant (ProductVariant): the variant to price.
        within_bundle (bool): whether the price is inside a bundle.

    Returns:
        Discount | None: the winning discount, or None when none apply.
    """
    candidates = _discounts_for_variant(variant, within_bundle)
    if not candidates:
        return None
    base = _money(variant.price)
    candidates.sort(key=lambda discount: -discount.priority)
    top_priority = candidates[0].priority
    return min(
        (discount for discount in candidates if discount.priority == top_priority),
        key=lambda discount: _apply_discount(base, discount),
    )


def get_effective_price(variant, coupon=None, within_bundle=False):
    """Return the effective unit price of a variant.

    The effective price is the current catalogue price reduced by the best
    applicable discount and, when a coupon is supplied, that coupon's value
    (subject to the coupon's stacking rule). All money values are recomputed
    server-side here — a client never supplies a price.

    The discount-reduced result is cached per variant and refreshed whenever
    a discount or variant price changes (via the generation counter). The
    coupon adjustment is applied on top at call time rather than cached,
    because coupon state (redemption counts) is far more volatile.

    Args:
        variant (ProductVariant): the variant to price.
        coupon (Coupon | None): an optional coupon to apply.
        within_bundle (bool): whether the price is inside a bundle.

    Returns:
        dict: ``variant``, ``base_price``, ``price``, ``discount``,
            ``discount_type``, ``discount_name``, ``badge_text``,
            ``coupon_discount``, and ``within_bundle``, with money as strings.
    """
    base = _money(variant.price)
    discounted, discount_data = _discount_price(variant, within_bundle, base)
    coupon_savings = _coupon_adjustment(variant, discounted, base, coupon)
    final_price = discounted - coupon_savings
    return {
        "variant": variant.pk,
        "base_price": str(base),
        "price": str(final_price),
        "discount": str(_discount_savings(base, final_price)),
        "discount_type": discount_data.get("discount_type"),
        "discount_name": discount_data.get("discount_name"),
        "badge_text": discount_data.get("badge_text"),
        "coupon_discount": str(coupon_savings),
        "within_bundle": within_bundle,
    }


def _discount_price(variant, within_bundle, base):
    """Return the price after the best discount, using the cache when warm.

    Args:
        variant (ProductVariant): the variant to price.
        within_bundle (bool): whether the price is inside a bundle.
        base (Decimal): the variant's base price.

    Returns:
        tuple: ``(discounted_price, discount_metadata)`` where the metadata
            is a dict of ``discount_type``, ``discount_name``, ``badge_text``.
    """
    generation = cache.get_discount_generation()
    cached = cache.get_cached_effective_price(variant.pk, within_bundle, generation)
    if cached is not None:
        return Decimal(cached["price"]), cached

    discount = _best_discount(variant, within_bundle)
    if discount is None:
        metadata = {
            "discount_type": None,
            "discount_name": None,
            "badge_text": None,
            "price": str(base),
        }
        price = base
    else:
        price = _apply_discount(base, discount)
        metadata = {
            "discount_type": discount.discount_type,
            "discount_name": discount.name,
            "badge_text": discount.badge_text,
            "price": str(price),
        }
    cache.cache_effective_price(variant.pk, within_bundle, generation, metadata)
    return price, metadata


def _discount_savings(price, final_price):
    """Return the amount saved between an original and final price.

    Args:
        price (Decimal): the original price.
        final_price (Decimal): the price actually charged.

    Returns:
        Decimal: the savings, rounded to a penny.
    """
    return (price - final_price).quantize(_PENNY, rounding=ROUND_HALF_UP)


def _coupon_applies_to_variant(coupon, variant):
    """Return whether a coupon's product/category restrictions cover a variant.

    A coupon with neither ``applies_to_products`` nor ``applies_to_categories``
    is unrestricted and applies to every product. Otherwise it applies only
    when the product is listed, or its category is listed, as an allowed target.

    Args:
        coupon (Coupon): the coupon.
        variant (ProductVariant): the variant being priced.

    Returns:
        bool: True when the coupon may discount this variant's product.
    """
    product = variant.product
    if coupon.applies_to_products.exists():
        if coupon.applies_to_products.filter(pk=product.pk).exists():
            return True
        return False
    if coupon.applies_to_categories.exists():
        category_id = product.category_id
        if (
            category_id is not None
            and coupon.applies_to_categories.filter(pk=category_id).exists()
        ):
            return True
        return False
    return True


def _coupon_adjustment(variant, discounted, base, coupon):
    """Return a coupon's price reduction, honoring stacking and scope rules.

    A coupon contributes to a unit price only when it is intrinsically
    applicable (active, in window), its product/category restrictions cover
    the variant, and — where an automatic discount has already reduced the
    price — the coupon is marked ``stackable_with_discounts``. A
    ``free_shipping`` coupon reduces only shipping, which is outside this
    unit-price function's scope, so it contributes nothing here.

    Args:
        variant (ProductVariant): the variant being priced.
        discounted (Decimal): the price after any automatic discount.
        base (Decimal): the variant's base (undiscounted) price.
        coupon (Coupon | None): the coupon to apply.

    Returns:
        Decimal: the coupon savings (capped at the discounted price).
    """
    if coupon is None:
        return Decimal("0.00")
    if not coupon.is_active or not _in_window(coupon.starts_at, coupon.ends_at):
        return Decimal("0.00")
    if not _coupon_applies_to_variant(coupon, variant):
        return Decimal("0.00")
    if coupon.discount_type == "free_shipping":
        return Decimal("0.00")
    if discounted < base and not coupon.stackable_with_discounts:
        return Decimal("0.00")
    amount = _money(coupon.value)
    if coupon.discount_type == "percent":
        savings = (discounted * amount) / _PERCENT_BASE
    else:
        savings = amount
    return min(savings, discounted).quantize(_PENNY, rounding=ROUND_HALF_UP)


def effective_unit_price(variant, within_bundle=False):
    """Return the effective unit price of a variant as a ``Decimal``.

    Convenience wrapper for callers (notably bundle pricing) that only need
    the numeric unit price rather than the display breakdown.

    Args:
        variant (ProductVariant): the variant to price.
        within_bundle (bool): whether the price is inside a bundle.

    Returns:
        Decimal: the effective unit price.
    """
    base = _money(variant.price)
    price, _ = _discount_price(variant, within_bundle, base)
    return price


# Discount CRUD


def create_discount(*, name, scope, discount_type, value, starts_at, **kwargs):
    """Create a discount, validating the scope has a matching relation.

    Args:
        name (str): the discount name.
        scope (str): the discount scope.
        discount_type (str): ``percent`` or ``fixed``.
        value (Decimal): the discount value or percentage.
        starts_at (datetime): the window start.
        **kwargs: additional Discount fields and scoped relation id lists.

    Returns:
        Discount: the created discount.

    Raises:
        ValidationError: if the scope has no matching relation populated.
    """
    relations = _extract_relations(kwargs)
    bundle_id = kwargs.get("bundle")
    if scope == "bundle" and isinstance(bundle_id, int):
        from apps.bundles.models import Bundle

        kwargs["bundle"] = Bundle.objects.filter(pk=bundle_id).first()
    _validate_scope_relations(scope, relations, kwargs.get("bundle"))
    discount = Discount(
        name=name,
        scope=scope,
        discount_type=discount_type,
        value=value,
        starts_at=starts_at,
        **kwargs,
    )
    with transaction.atomic():
        discount.full_clean()
        discount.save()
        _set_relations(discount, relations)
    return discount


def _validate_scope_relations(scope, relations, bundle_id):
    """Require a scope-specific relation for non-sitewide discounts.

    Args:
        scope (str): the discount scope.
        relations (dict): relation id lists keyed by relation name.
        bundle_id: the selected bundle id, when the scope is ``bundle``.

    Raises:
        ValidationError: if the scope's relation is empty or the bundle scope
            has no bundle selected.
    """
    if scope == "bundle":
        if not bundle_id:
            raise ValidationError({"bundle": "Select a bundle for this scope."})
        return
    if scope == "sitewide":
        return
    relation_key = _scope_relation_key(scope)
    if not relations.get(relation_key):
        raise ValidationError(
            f"Select at least one {scope} for a {scope}-scoped discount."
        )


def _scope_relation_key(scope):
    """Return the relation field name matching a scope value.

    Args:
        scope (str): the discount scope value.

    Returns:
        str: the matching many-to-many relation name.
    """
    return {
        "variant": "variants",
        "product": "products",
        "category": "categories",
        "brand": "brands",
    }[scope]


def _extract_relations(data):
    """Pull many-to-many scope id lists out of a field dict.

    Args:
        data (dict): the fields dict, possibly containing relation id lists.

    Returns:
        dict: the relation id lists, keyed by relation name.
    """
    relations = {}
    for relation in ("variants", "products", "categories", "brands"):
        if relation in data:
            relations[relation] = data.pop(relation)
    return relations


def _set_relations(discount, relations):
    """Persist a discount's many-to-many scope relations from id lists.

    Args:
        discount (Discount): the discount being created.
        relations (dict): relation id lists keyed by relation name.
    """
    for relation, ids in relations.items():
        if ids:
            getattr(discount, relation).set(ids)


def update_discount(discount, **data):
    """Update a discount's scalar fields and scope relations.

    Args:
        discount (Discount): the discount to update.
        **data: fields to update, including optional relation id lists.

    Returns:
        Discount: the updated discount.
    """
    relations = _extract_relations(data)
    for relation, ids in relations.items():
        if ids:
            getattr(discount, relation).set(ids)
        else:
            getattr(discount, relation).clear()
    for field, value in data.items():
        setattr(discount, field, value)
    discount.full_clean()
    discount.save()
    return discount


# Coupon services


def validate_coupon(coupon, user=None, subtotal=None):
    """Return whether a coupon is currently usable and its discount metadata.

    Checks intrinsic validity (active, in window, and not exhausted against
    the global and per-user usage limits) and, when a ``subtotal`` is given,
    that it meets the coupon's ``min_order_value``. Product/category
    restrictions and the exact charged amount are evaluated later at checkout
    against the cart contents.

    Args:
        coupon (Coupon): the coupon to validate.
        user (User | None): the applying user, for per-user limits.
        subtotal (Decimal | None): the cart subtotal, when known.

    Returns:
        dict: ``valid``, ``code``, ``discount_type``, ``value``,
            ``min_order_value``, and ``reason`` (None when valid).
    """
    if not coupon.is_active:
        return _invalid(coupon, "Coupon is not active.")
    if not _in_window(coupon.starts_at, coupon.ends_at):
        return _invalid(coupon, "Coupon is outside its valid dates.")
    if subtotal is not None and _money(subtotal) < _money(coupon.min_order_value):
        return _invalid(coupon, "Order value is below the coupon minimum.")
    if coupon.usage_limit_total is not None:
        used = coupon.redemptions.count()
        if used >= coupon.usage_limit_total:
            return _invalid(coupon, "Coupon usage limit has been reached.")
    if user is not None:
        used_by_user = CouponRedemption.objects.filter(coupon=coupon, user=user).count()
        if used_by_user >= coupon.usage_limit_per_user:
            return _invalid(coupon, "Coupon has already been used by this user.")
    return {
        "valid": True,
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "value": str(coupon.value) if coupon.value is not None else None,
        "min_order_value": str(coupon.min_order_value),
        "reason": None,
    }


def validate_coupon_code(code, user=None, subtotal=None):
    """Look up a coupon by code and validate it.

    Args:
        code (str): the coupon code.
        user (User | None): the applying user.
        subtotal (Decimal | None): the cart subtotal, when known.

    Returns:
        dict: the validation result (``valid`` False with ``reason`` when the
            code does not exist or is not usable).
    """
    coupon = Coupon.objects.filter(code__iexact=code.strip()).first()
    if coupon is None:
        return {
            "valid": False,
            "code": code,
            "discount_type": None,
            "value": None,
            "min_order_value": None,
            "reason": "Unknown coupon code.",
        }
    return validate_coupon(coupon, user=user, subtotal=subtotal)


def _invalid(coupon, reason):
    """Return a fixed validation-failure shape.

    Args:
        coupon (Coupon): the coupon that failed validation.
        reason (str): the human-readable reason.

    Returns:
        dict: the validation result with ``valid`` False.
    """
    return {
        "valid": False,
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "value": str(coupon.value) if coupon.value is not None else None,
        "min_order_value": str(coupon.min_order_value),
        "reason": reason,
    }


def record_discount_redemption(discount):
    """Record an automatic discount application, bumping its usage counter.

    Called at order confirmation whenever a ``Discount`` actually reduces a
    checkout total. Locks the discount row and increments ``redemption_count``,
    refusing to do so once ``max_redemptions`` is reached, so the budget is
    enforced exactly under concurrency.

    Args:
        discount (Discount): the discount that was applied.

    Returns:
        Discount: the refreshed discount (with the bumped counter).

    Raises:
        ValidationError: if the discount's redemption limit is reached.
    """
    with transaction.atomic():
        locked = Discount.objects.select_for_update().get(pk=discount.pk)
        if (
            locked.max_redemptions is not None
            and locked.redemption_count >= locked.max_redemptions
        ):
            raise ValidationError("Discount redemption limit has been reached.")
        Discount.objects.filter(pk=locked.pk).update(
            redemption_count=F("redemption_count") + 1
        )
        return Discount.objects.get(pk=locked.pk)


def record_redemption(coupon, user=None, order=None):
    """Record that a coupon has been used, enforcing its usage limits atomically.

    The coupon row is locked (``select_for_update``) and its limits are
    re-checked inside the same transaction as the insert, so two concurrent
    checkouts cannot both pass a usage check and together overshoot the
    ``usage_limit_total`` / ``usage_limit_per_user`` budget.

    Args:
        coupon (Coupon): the coupon being redeemed.
        user (User | None): the redeeming user, when logged in.
        order (Order | None): the order the coupon was redeemed against, when
            it exists at redemption time (i.e. a confirmed order).

    Returns:
        CouponRedemption: the created redemption record.

    Raises:
        ValidationError: if the coupon is not usable (inactive, out of
            window, or its global or per-user usage limit is exhausted).
    """
    with transaction.atomic():
        locked = Coupon.objects.select_for_update().get(pk=coupon.pk)
        if not locked.is_active:
            raise ValidationError("Coupon is not active.")
        if not _in_window(locked.starts_at, locked.ends_at):
            raise ValidationError("Coupon is outside its valid dates.")
        if locked.usage_limit_total is not None:
            used = locked.redemptions.count()
            if used >= locked.usage_limit_total:
                raise ValidationError("Coupon usage limit has been reached.")
        if user is not None:
            used_by_user = CouponRedemption.objects.filter(
                coupon=locked, user=user
            ).count()
            if used_by_user >= locked.usage_limit_per_user:
                raise ValidationError("Coupon has already been used by this user.")
        return CouponRedemption.objects.create(coupon=locked, user=user, order=order)
