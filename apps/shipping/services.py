"""Business logic for the shipping app.

Two pure functions capture the platform's delivery rules:

- ``calculate_shipping_fee`` — the single source of truth for what a cart is
  charged for delivery to a zone, priced by billable weight with an optional
  free-shipping threshold.
- ``select_fulfillment_warehouse`` — the computable rule that picks which
  warehouse fulfils an order line destined for a zone, used when reserving
  stock at checkout.

Neither function writes to the database; the checkout/shipping views and the
future orders flow call them and act on the result. All money and weight math
stays in ``Decimal`` so the fee never suffers float rounding.
"""

from decimal import Decimal

from django.db.models import F

from apps.core.models import SiteConfig
from apps.inventory.models import Inventory


def _as_money(value):
    """Return ``value`` as an exact ``Decimal``.

    ``DecimalField`` values are ``Decimal`` under PostgreSQL but plain
    strings under the in-memory SQLite used by tests, so any arithmetic on a
    money or weight field must pass through here first. Guarding at the
    boundary keeps the fee exact on both backends.

    Args:
        value: a ``DecimalField`` value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def billable_kg(variant):
    """Return the billable weight in kg for a variant.

    The billable weight is the larger of the physical ``package_weight`` and
    the volumetric weight derived from ``package_dimensions``, so a light but
    bulky item is priced by the space it occupies. Dimensions are
    centimetres and the divisor is the configurable
    ``volumetric_weight_divisor`` (default 5000) per the standard
    volumetric-weight formula.

    Args:
        variant (ProductVariant): the variant to measure.

    Returns:
        Decimal: the billable weight in kilograms.

    Raises:
        ValueError: if the variant measures to zero both ways — no physical
            weight and no dimensions — because such a line would otherwise
            ship for the base fee alone. The catalogue must define package
            data before a variant is quotable or shippable.
    """
    divisor = _as_money(
        SiteConfig.load().settings.get("volumetric_weight_divisor", 5000)
    )
    actual = _as_money(variant.package_weight or 0)
    dims = variant.package_dimensions or {}
    length = Decimal(str(dims.get("length", 0)))
    width = Decimal(str(dims.get("width", 0)))
    height = Decimal(str(dims.get("height", 0)))
    volumetric = (length * width * height) / divisor
    if actual == 0 and volumetric == 0:
        raise ValueError(
            f"variant {variant.sku} has no package weight or dimensions; "
            "set package_weight or package_dimensions before shipping"
        )
    return max(actual, volumetric)


def calculate_shipping_fee(zone, lines):
    """Return the shipping fee for a cart destined for a delivery zone.

    ``lines`` is an iterable of objects exposing ``variant`` and
    ``quantity`` (a future ``CartItem``, or the quote lines built by the
    quote endpoint); each line's billable weight is the greater of its
    physical and volumetric weight, multiplied by its quantity. The fee is
    ``base_fee + per_kg_rate * total_billable_weight`` and is waived when the
    subtotal reaches the zone's free-shipping threshold.

    The subtotal used for the threshold check is always derived server-side
    from current variant prices — never accepted from the caller — so a
    shopper cannot waive delivery by inflating their own subtotal.

    Args:
        zone (DeliveryZone): the destination zone pricing the delivery.
        lines (Iterable): line objects with ``variant`` and ``quantity``.

    Returns:
        Decimal: the shipping fee, rounded to two decimal places.

    Raises:
        ValueError: if any line's variant has no package weight or dimensions
            to price against.
    """
    # The free-shipping threshold compares the raw catalogue price. Discounted
    # pricing does not exist yet; when it lands the comparison must use the
    # effective (post-discount) subtotal so a discounted cart is not wrongly
    # excluded from free shipping.
    computed_subtotal = Decimal("0")
    total_billable_weight = Decimal("0")
    for line in lines:
        quantity = line.quantity
        total_billable_weight += billable_kg(line.variant) * quantity
        computed_subtotal += _as_money(line.variant.price) * quantity

    if zone.free_shipping_threshold is not None and computed_subtotal >= _as_money(
        zone.free_shipping_threshold
    ):
        return Decimal("0.00")

    fee = _as_money(zone.base_fee) + (
        _as_money(zone.per_kg_rate) * total_billable_weight
    )
    return fee.quantize(Decimal("0.01"))


def select_fulfillment_warehouse(variant, delivery_zone, quantity):
    """Pick the warehouse that should fulfil a variant for a delivery zone.

    The zone's ``WarehouseZonePriority`` ranking is tried in ascending order
    and the first ranked, active warehouse with at least ``quantity`` units
    available wins. If none of the ranked warehouses can cover the order, the
    function falls back to any active warehouse with sufficient available
    stock (largest first; a stable id tie-break). Inactive/decommissioned
    warehouses are never chosen — a mothballed site cannot receive new
    fulfilment routing.

    All candidate stock for the variant is fetched in a single query and
    matched to the zone's ranking in Python, so the cost is two queries
    however many warehouses the zone ranks.

    Args:
        variant (ProductVariant): the variant being fulfilled.
        delivery_zone (DeliveryZone): the destination delivery zone.
        quantity (int): the number of units needed.

    Returns:
        Warehouse | None: the chosen warehouse, or ``None`` when no active
            warehouse holds enough available stock.
    """
    priorities = list(
        delivery_zone.warehouse_priorities.filter(warehouse__is_active=True)
        .order_by("priority", "id")
        .select_related("warehouse")
    )
    stock_by_warehouse = {
        row.warehouse_id: row
        for row in Inventory.objects.filter(variant=variant, warehouse__is_active=True)
        .annotate(available_units=F("quantity") - F("reserved"))
        .select_related("warehouse")
    }
    for priority in priorities:
        row = stock_by_warehouse.get(priority.warehouse_id)
        if row is not None and row.available_units >= quantity:
            return row.warehouse

    candidates = [
        row for row in stock_by_warehouse.values() if row.available_units >= quantity
    ]
    if not candidates:
        return None
    return max(
        candidates, key=lambda row: (row.available_units, row.warehouse_id)
    ).warehouse
