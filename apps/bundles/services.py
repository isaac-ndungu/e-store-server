"""Business logic for the bundles app.

The central service is ``get_bundle_price`` — the single source of truth for
the displayed and charged price of a bundle. It derives the regular price from
the current component-variant prices and applies the bundle's discount, all in
exact ``Decimal`` arithmetic, and caches the result per slug.

The price is always computed server-side here from live catalogue prices and
the bundle's own discount fields; a value supplied by a client is display
data, never an input to what actually gets charged.

CRUD helpers keep the views thin, mirroring the catalogue and collections
apps. ``variant`` referencing is handled at the service layer so the price
calculation only ever sees concrete variant prices.
"""

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from apps.bundles import cache
from apps.bundles.models import Bundle, BundleItem

logger = logging.getLogger(__name__)

# Base used by percent discounts to keep the math in exact decimal form.
_PERCENT_BASE = Decimal("100")


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


def _unique_slug(base, exclude_pk=None):
    """Return a slug unique among bundles.

    Args:
        base (str): the desired slug value.
        exclude_pk (int | None): a bundle pk to exclude from the check (for
            updates).

    Returns:
        str: a unique slug.
    """
    slug = slugify(base) or "bundle"
    candidate = slug
    counter = 1
    queryset = Bundle.objects.all()
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    while queryset.filter(slug=candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1
    return candidate


# Bundle CRUD


def create_bundle(
    *, name, slug=None, discount_type, discount_value, items=None, **kwargs
):
    """Create a bundle with its component items atomically.

    Wraps the bundle and its items in ``transaction.atomic()`` so a failure
    partway through (e.g. a missing product) rolls back the bundle and any
    already-created items.

    Args:
        name (str): the bundle name.
        slug (str | None): explicit slug, or None to auto-generate.
        discount_type (str): ``percent`` or ``fixed``.
        discount_value (Decimal): the discount amount or percentage.
        items (list[dict] | None): bundle-item data dicts to create.
        **kwargs: additional Bundle fields.

    Returns:
        Bundle: the newly created bundle with its items.

    Raises:
        ValidationError: if a bundle item references a product that has no
            variant (a component must be priceable) or imports a variant that
            does not belong to its product.
    """
    if not slug:
        slug = _unique_slug(name)

    with transaction.atomic():
        bundle = Bundle.objects.create(
            name=name,
            slug=slug,
            discount_type=discount_type,
            discount_value=discount_value,
            **kwargs,
        )
        if items:
            for item_data in items:
                _validate_item(item_data)
                BundleItem.objects.create(bundle=bundle, **item_data)
    return bundle


def update_bundle(bundle, **data):
    """Update a bundle's fields.

    Items are managed through their own service methods.

    Args:
        bundle (Bundle): the bundle to update.
        **data: fields to update.

    Returns:
        Bundle: the updated bundle.
    """
    if "name" in data and "slug" not in data:
        data["slug"] = _unique_slug(data["name"], exclude_pk=bundle.pk)
    for field, value in data.items():
        setattr(bundle, field, value)
    bundle.save()
    return bundle


def add_bundle_item(
    bundle, *, product_id, variant_id=None, quantity=1, is_optional=False
):
    """Add a component to a bundle.

    Args:
        bundle (Bundle): the bundle to extend.
        product_id (int): the component product id.
        variant_id (int | None): the component variant id, when specific.
        quantity (int): how many of the component are included.
        is_optional (bool): whether the component may be dropped by the buyer.

    Returns:
        BundleItem: the created item.

    Raises:
        ValidationError: if the product is unpriceable or the variant does not
            belong to the product.
    """
    data = {
        "product_id": product_id,
        "variant_id": variant_id,
        "quantity": quantity,
        "is_optional": is_optional,
    }
    _validate_item(data)
    return BundleItem.objects.create(bundle=bundle, **data)


def _validate_item(data):
    """Reject a bundle item that cannot be priced cleanly.

    A component must resolve to a concrete price so the bundle can be quoted.
    A product-only item (no variant) is allowed only when the product has an
    active variant to price against; a variant that is supplied must belong to
    the item's product.

    Args:
        data (dict): the item data to validate.

    Raises:
        ValidationError: if the component cannot be priced.
    """
    from apps.catalog.models import Product, ProductVariant

    variant_id = data.get("variant_id")
    product_id = data.get("product_id")
    if variant_id is not None:
        variant = ProductVariant.objects.filter(pk=variant_id).first()
        if variant is None or variant.product_id != product_id:
            raise ValidationError(
                "Bundle item variant must belong to the item's product."
            )
    else:
        if not Product.objects.filter(pk=product_id, variants__is_active=True).exists():
            raise ValidationError(
                "Bundle item product must have at least one active variant."
            )


# Bundle pricing


def get_bundle_price(bundle):
    """Return the price breakdown for a bundle.

    The regular price is the sum of each component's billable price times its
    quantity, all derived server-side from the current variant prices. The
    discount — a percentage of the regular total or a fixed amount — is applied
    to produce the bundle price. A fixed discount is capped at the regular
    total so the bundle price never goes negative.

    A component with no specific variant is priced at the lowest price among
    its product's active variants — the storefront convention for a product
    offered in several configurations. Optional items are included in the
    quoted price; dropping them is a checkout-time decision.

    The result is cached per slug and invalidated on any bundle/item change.

    Args:
        bundle (Bundle): the bundle to price.

    Returns:
        dict: ``regular_price``, ``discount``, ``price``, and per-item
            ``items`` detail, with money as strings.

    Raises:
        ValidationError: if any component cannot be priced.
    """
    cached = cache.get_cached_bundle_price(bundle.slug)
    if cached is not None:
        return cached

    price_data = _compute_bundle_price(bundle)
    cache.cache_bundle_price(bundle.slug, price_data)
    return price_data


def _compute_bundle_price(bundle):
    """Compute a bundle's price breakdown from live catalogue data.

    Args:
        bundle (Bundle): the bundle to price.

    Returns:
        dict: the price breakdown with money as decimal strings.

    Raises:
        ValidationError: if any component cannot be priced.
    """
    item_rows = list(bundle.items.select_related("product", "variant").order_by("pk"))
    if not item_rows:
        raise ValidationError("A bundle must have at least one item to be priced.")

    regular_total = Decimal("0.00")
    effective_total = Decimal("0.00")
    items_detail = []
    for item in item_rows:
        source = _item_source_variant(item)
        regular_unit = source.price
        unit_price = _item_unit_price(item)
        regular_total += regular_unit * item.quantity
        effective_total += unit_price * item.quantity
        items_detail.append(
            {
                "product": item.product_id,
                "product_name": item.product.name,
                "variant": item.variant_id,
                "quantity": item.quantity,
                "is_optional": item.is_optional,
                "unit_price": str(unit_price),
                "regular_unit_price": str(regular_unit),
                "line_total": str(unit_price * item.quantity),
            }
        )

    discount = _discount_amount(bundle, effective_total)

    return {
        "slug": bundle.slug,
        "regular_price": str(regular_total),
        "discount": str(discount),
        "price": str(effective_total - discount),
        "items": items_detail,
    }


def _item_source_variant(item):
    """Return the catalog variant a bundle item should be priced from.

    A variant-specific item prices from that variant. A product-only item
    prices from the product's cheapest active variant.

    Args:
        item (BundleItem): the bundle item.

    Returns:
        ProductVariant: the source variant.

    Raises:
        ValidationError: if the item has no priceable source.
    """
    if item.variant_id is not None:
        if item.variant is None:
            raise ValidationError(
                "Bundle item variant was removed and can no longer be priced."
            )
        return item.variant
    cheapest = (
        item.product.variants.filter(is_active=True).order_by("price", "pk").first()
    )
    if cheapest is None:
        raise ValidationError(
            "Bundle item product has no active variant to price against."
        )
    return cheapest


def _item_unit_price(item):
    """Return the billable unit price for a bundle item.

    A variant-specific item uses that variant's effective price (after any
    discount that opts into bundles). A product-only item uses the lowest
    price among the product's active variants, likewise discount-adjusted.
    The effective price comes from the promotions service so a component
    inside a bundle is never double-discounted by an individual-item
    promotion unless it is explicitly marked to apply within bundles.

    Args:
        item (BundleItem): the bundle item.

    Returns:
        Decimal: the unit price for the component.

    Raises:
        ValidationError: if the item has no priceable source.
    """
    from apps.promotions.services import effective_unit_price

    variant = _item_source_variant(item)
    return effective_unit_price(variant, within_bundle=True)


def _discount_amount(bundle, regular_total):
    """Return the discount to apply for a bundle on a regular total.

    The result is rounded to two decimal places — the precision of the money
    fields — so a percent discount on a large total (which can produce
    fractional cents) stays exact and consistent across callers.

    Args:
        bundle (Bundle): the bundle.
        regular_total (Decimal): the undiscounted total.

    Returns:
        Decimal: the discount amount (capped at the regular total).
    """
    value = _money(bundle.discount_value)
    if bundle.discount_type == "percent":
        discount = (regular_total * value) / _PERCENT_BASE
    else:
        discount = value
    return min(discount, regular_total).quantize(Decimal("0.01"))
