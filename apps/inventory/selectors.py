"""Selectors for the inventory app.

Optimized read-only query helpers for stock availability and admin listing.
Views stay thin and delegate here so the query patterns (``select_related``,
aggregation across warehouses) live in one place.
"""

from django.db.models import F, Sum

from apps.inventory.models import Inventory, SerialUnit, StockReservation


def get_inventory_rows(variant):
    """Return per-warehouse inventory rows for a variant.

    Args:
        variant (ProductVariant): the variant to inspect.

    Returns:
        QuerySet: ``Inventory`` rows for active warehouses with the warehouse
            pre-fetched, ordered by warehouse.
    """
    return (
        Inventory.objects.filter(variant=variant, warehouse__is_active=True)
        .select_related("warehouse")
        .order_by("warehouse_id")
    )


def get_variant_availability(variant):
    """Compute per-warehouse and aggregate availability for a variant.

    All active warehouses are aggregated explicitly — a variant has one
    ``Inventory`` row per warehouse, so its total stock is never a single
    row's value. Inactive (decommissioned) warehouses are excluded. Each
    warehouse reports its reorder point and whether it is currently low.

    Args:
        variant (ProductVariant): the variant to inspect.

    Returns:
        dict: ``{"variant": {"id": ..., "sku": ...},
        "total_quantity": int, "total_reserved": int, "total_available": int,
        "is_low_stock": bool, "per_warehouse": [{warehouse_id,
        warehouse_name, quantity, reserved, available, low_stock_threshold,
        is_low_stock}, ...]}``
    """
    rows = list(get_inventory_rows(variant))
    per_warehouse = [
        {
            "warehouse_id": row.warehouse_id,
            "warehouse_name": row.warehouse.name,
            "quantity": row.quantity,
            "reserved": row.reserved,
            "available": row.available,
            "low_stock_threshold": row.low_stock_threshold,
            "is_low_stock": row.is_low_stock,
        }
        for row in rows
    ]
    return {
        "variant": {"id": variant.pk, "sku": variant.sku},
        "total_quantity": sum(row["quantity"] for row in per_warehouse),
        "total_reserved": sum(row["reserved"] for row in per_warehouse),
        "total_available": sum(row["available"] for row in per_warehouse),
        "is_low_stock": any(row["is_low_stock"] for row in per_warehouse),
        "per_warehouse": per_warehouse,
    }


def get_available_quantity(variant):
    """Return the total units available across all active warehouses.

    Args:
        variant (ProductVariant): the variant to inspect.

    Returns:
        int: the sum of ``quantity - reserved`` over every active warehouse.
    """
    aggregate = Inventory.objects.filter(
        variant=variant, warehouse__is_active=True
    ).aggregate(available=Sum(F("quantity") - F("reserved")))
    return aggregate["available"] or 0


def list_inventory(variant_id=None, warehouse_id=None):
    """Return inventory rows for admin listing.

    Args:
        variant_id (int | None): optional variant primary key filter.
        warehouse_id (int | None): optional warehouse primary key filter.

    Returns:
        QuerySet: inventory rows with variant, product, and warehouse
            pre-fetched.
    """
    queryset = Inventory.objects.select_related(
        "variant__product", "warehouse"
    ).order_by("warehouse_id", "variant_id")
    if variant_id is not None:
        queryset = queryset.filter(variant_id=variant_id)
    if warehouse_id is not None:
        queryset = queryset.filter(warehouse_id=warehouse_id)
    return queryset


def list_reservations(status=None):
    """Return reservation rows for admin listing.

    Args:
        status (str | None): optional ``StockReservation.STATUS_CHOICES``
            value to filter by.

    Returns:
        QuerySet: reservations with inventory, variant, and warehouse
            pre-fetched.
    """
    queryset = StockReservation.objects.select_related(
        "inventory__variant__product", "inventory__warehouse"
    ).order_by("-reserved_at")
    if status is not None:
        queryset = queryset.filter(status=status)
    return queryset


def list_serial_units(variant_id=None, warehouse_id=None, status=None):
    """Return serial units for admin listing.

    Args:
        variant_id (int | None): optional variant primary key filter.
        warehouse_id (int | None): optional warehouse primary key filter.
        status (str | None): optional ``SerialUnit.STATUS_CHOICES`` value
            filter.

    Returns:
        QuerySet: serial units with variant and warehouse pre-fetched.
    """
    queryset = SerialUnit.objects.select_related("variant__product", "warehouse")
    if variant_id is not None:
        queryset = queryset.filter(variant_id=variant_id)
    if warehouse_id is not None:
        queryset = queryset.filter(warehouse_id=warehouse_id)
    if status is not None:
        queryset = queryset.filter(status=status)
    return queryset
