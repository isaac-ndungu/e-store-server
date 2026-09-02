"""Business logic for the inventory app.

All stock movements flow through a small set of functions:

- ``receive_stock`` — the only way to increase count-based stock (and the
  only way to stock non-serialized products).
- ``receive_serial_units`` — for serialized products, registers individual
  units and feeds the count ledger from them, so the two never diverge.
- ``create_reservation`` — holds a quantity for an in-flight order line,
  raising the linked ``Inventory`` reserved count and marking (and owning)
  specific serial units reserved, under a row lock so concurrent checkouts
  cannot oversell.
- ``fulfill_reservation`` — turns a hold into a real deduction, marking the
  reservation's own serial units sold.
- ``release_reservation`` — returns a hold's stock to available after a
  failure or expiry.
- ``update_serial_unit_status`` — the only allowed manual ``SerialUnit``
  transitions, reconciling the count ledger as units move between
  sellable and non-sellable states.

Counts are never decremented directly. The reservation methods re-check the
reservation's current status under lock and no-op on a terminal state, which
makes fulfilment and release safe to retry without double-moving stock.
"""

from collections import Counter
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.core.models import SiteConfig
from apps.inventory.models import (
    Inventory,
    SerialUnit,
    StockMovementLog,
    StockReservation,
    Warehouse,
)

# Manual transitions an ops user may apply to a serial unit. ``reserved`` and
# ``sold`` units are owned by the reservation lifecycle; ``defective`` is a
# terminal state so a withdrawn unit cannot silently re-enter sellable stock.
_SERIAL_TRANSITIONS = {
    "in_stock": {"returned", "defective"},
    "returned": {"in_stock", "defective"},
}


def _variant_tracks_serial_numbers(variant_id):
    """Return whether the variant's product is tracked by serial number.

    Args:
        variant_id (int): the product variant primary key.

    Returns:
        bool: True when serial numbers are tracked for the variant.
    """
    return (
        ProductVariant.objects.filter(pk=variant_id)
        .values_list("product__tracks_serial_numbers", flat=True)
        .first()
        or False
    )


def get_reservation_grace():
    """Return the reservation grace period in minutes from site settings.

    Returns:
        int: the configured grace period, defaulting to 15 minutes.
    """
    return SiteConfig.load().settings.get("stock_reservation_grace_minutes", 15)


def _ensure_active_warehouse(warehouse):
    """Raise if a warehouse is not available for new stock.

    Args:
        warehouse (Warehouse): the warehouse to check.

    Raises:
        ValidationError: if the warehouse is ``is_active=False``.
    """
    if not warehouse.is_active:
        raise ValidationError(f"Warehouse '{warehouse.name}' is not active.")


def _log_movement(
    *,
    user,
    action,
    inventory=None,
    variant=None,
    warehouse=None,
    quantity_change=0,
    reserved_change=0,
    serial_number="",
    from_status="",
    to_status="",
):
    """Write one stock-movement audit entry.

    Called inside the same transaction as the mutation it records.

    Args:
        user (User | None): the acting user, or None for background work.
        action (str): one of ``StockMovementLog.ACTION_CHOICES``.
        inventory (Inventory | None): the affected inventory row.
        variant (ProductVariant | None): the affected variant.
        warehouse (Warehouse | None): the affected warehouse.
        quantity_change (int): signed change to ``Inventory.quantity``.
        reserved_change (int): signed change to ``Inventory.reserved``.
        serial_number (str): the unit involved in a per-unit transition.
        from_status (str): the unit's status before a transition.
        to_status (str): the unit's status after a transition.
    """
    StockMovementLog.objects.create(
        user=user if user is not None and user.is_authenticated else None,
        inventory=inventory,
        variant=variant or (inventory.variant if inventory is not None else None),
        warehouse=warehouse or (inventory.warehouse if inventory is not None else None),
        action=action,
        quantity_change=quantity_change,
        reserved_change=reserved_change,
        serial_number=serial_number,
        from_status=from_status,
        to_status=to_status,
    )


def receive_stock(*, variant, warehouse, quantity, low_stock_threshold=None, user=None):
    """Add ``quantity`` units of a variant to a warehouse.

    Only increases stock: reducing stock must happen through the reservation
    lifecycle. Creates the ``Inventory`` row on first receipt, otherwise
    increments ``quantity`` under a row lock so concurrent received loads do
    not overwrite each other. Serialized products are stocked via
    ``receive_serial_units`` instead — a count receipt would leave the count
    ledger out of step with the registered units.

    Args:
        variant (ProductVariant): the variant being stocked.
        warehouse (Warehouse): the receiving warehouse.
        quantity (int): the number of units to add (must be positive).
        low_stock_threshold (int | None): threshold to set when creating or
            updating the row; ``None`` leaves the existing value untouched.
        user (User | None): the acting user, recorded in the movement log.

    Returns:
        Inventory: the updated inventory row.

    Raises:
        ValidationError: if ``quantity`` is not positive, the variant is
            serialized, or the warehouse is not active.
    """
    if quantity <= 0:
        raise ValidationError("Received quantity must be a positive integer.")
    if _variant_tracks_serial_numbers(variant.pk):
        raise ValidationError(
            "Serialized products are stocked by registering serial units, "
            "not with a count receipt."
        )
    _ensure_active_warehouse(warehouse)

    with transaction.atomic():
        inventory = (
            Inventory.objects.select_for_update()
            .filter(variant=variant, warehouse=warehouse)
            .first()
        )
        if inventory is None:
            inventory = Inventory.objects.create(
                variant=variant,
                warehouse=warehouse,
                quantity=quantity,
                low_stock_threshold=(
                    low_stock_threshold if low_stock_threshold is not None else 5
                ),
            )
        else:
            if low_stock_threshold is not None:
                inventory.low_stock_threshold = low_stock_threshold
            inventory.quantity = F("quantity") + quantity
            inventory.save(
                update_fields=["quantity", "low_stock_threshold", "updated_at"]
            )
            inventory.refresh_from_db()
        _log_movement(
            user=user,
            action="receive_count",
            inventory=inventory,
            quantity_change=quantity,
        )
    return inventory


def _serial_duplicates(serial_numbers):
    """Return serial numbers that appear more than once in a batch.

    Args:
        serial_numbers (list[str]): the batch to inspect.

    Returns:
        list[str]: duplicated values in the order they first appear.
    """
    counts = Counter(serial_numbers)
    return [value for value, count in counts.items() if count > 1]


def receive_serial_units(*, variant, warehouse, serial_numbers, user=None):
    """Register individual serial units as in-stock for a variant.

    Only valid for products that track serial numbers. Each serial number
    must be unique across the platform; duplicates within the batch or
    against existing registrations are rejected up front so a replayed scan
    returns a clear error instead of a database integrity failure. The count
    ledger is fed from the registered units (the ``Inventory`` row is created
    or incremented by the number of units) so count-based availability stays
    in step with what is physically registered.

    Args:
        variant (ProductVariant): the serialized variant.
        warehouse (Warehouse): where the units physically sit.
        serial_numbers (list[str]): the serial numbers to register.
        user (User | None): the acting user, recorded in the movement log.

    Returns:
        list[SerialUnit]: the created serial units.

    Raises:
        ValidationError: if the product is not serialized, the warehouse is
            not active, a duplicate serial number is supplied, or a racing
            registration collides mid-flight.
    """
    if not _variant_tracks_serial_numbers(variant.pk):
        raise ValidationError(
            "Serial units can only be registered for products that track "
            "serial numbers."
        )
    _ensure_active_warehouse(warehouse)
    if not serial_numbers:
        return []

    duplicates = _serial_duplicates(serial_numbers)
    if duplicates:
        raise ValidationError(
            {"serial_number": f"Duplicate serial numbers: {', '.join(duplicates)}"}
        )

    with transaction.atomic():
        inventory = (
            Inventory.objects.select_for_update()
            .filter(variant=variant, warehouse=warehouse)
            .first()
        )
        if inventory is None:
            inventory = Inventory.objects.create(
                variant=variant, warehouse=warehouse, quantity=0
            )

        existing = set(
            SerialUnit.objects.filter(serial_number__in=serial_numbers).values_list(
                "serial_number", flat=True
            )
        )
        if existing:
            raise ValidationError(
                {
                    "serial_number": (
                        "Already registered: "
                        + ", ".join(sorted(set(serial_numbers) & existing))
                    )
                }
            )

        try:
            units = SerialUnit.objects.bulk_create(
                [
                    SerialUnit(variant=variant, warehouse=warehouse, serial_number=sn)
                    for sn in serial_numbers
                ]
            )
        except IntegrityError as exc:
            raise ValidationError(
                "One or more serial numbers collided with an existing "
                "registration; refresh and re-verify the batch."
            ) from exc

        inventory.quantity = F("quantity") + len(serial_numbers)
        inventory.save(update_fields=["quantity", "updated_at"])
        inventory.refresh_from_db()
        _log_movement(
            user=user,
            action="receive_serial",
            inventory=inventory,
            quantity_change=len(serial_numbers),
        )
    return units


def create_reservation(*, variant, quantity, warehouse=None, user=None):
    """Hold ``quantity`` units of a variant for an in-flight order line.

    Runs under a ``select_for_update`` transaction. ``warehouse`` accepts a
    single ``Warehouse`` or a sequence of warehouses in allocation-priority
    order; when given, only those warehouses are considered and the first with
    enough available stock wins. When ``None``, the warehouse with the most
    available units that can cover the quantity wins. For serialized variants
    the chosen warehouse must also have that many in-stock serial units, and
    those units are immediately marked ``reserved`` and owned by the new
    reservation so later fulfil/release touches exactly this reservation's
    units.

    Args:
        variant (ProductVariant): the variant to reserve.
        quantity (int): the number of units to hold.
        warehouse (Warehouse | tuple[Warehouse] | list[Warehouse] | None):
            restricted candidate warehouse(s) in priority order, or ``None``
            to allow any active warehouse.
        user (User | None): unused for reservation creation (the reservation
            row is its own audit record).

    Returns:
        StockReservation: the created reservation.

    Raises:
        ValidationError: if ``quantity`` is not positive or no allowed
            warehouse has enough available units.
    """
    if quantity <= 0:
        raise ValidationError("Reservation quantity must be a positive integer.")

    warehouse_ids = None
    if warehouse is not None:
        candidate_warehouses = (
            [warehouse] if isinstance(warehouse, Warehouse) else list(warehouse)
        )
        warehouse_ids = [item.pk for item in candidate_warehouses]

    tracks_serial = _variant_tracks_serial_numbers(variant.pk)
    expires_at = timezone.now() + timedelta(minutes=get_reservation_grace())

    with transaction.atomic():
        queryset = (
            Inventory.objects.select_for_update()
            .filter(variant=variant, warehouse__is_active=True)
            .select_related("warehouse")
            .order_by("warehouse_id")
        )
        if warehouse_ids is not None:
            locked = list(queryset.filter(warehouse_id__in=warehouse_ids))
        else:
            locked = list(queryset)

        eligible = [row for row in locked if row.available >= quantity]
        if not eligible:
            raise ValidationError(
                f"Insufficient stock for variant {variant.pk}: "
                f"requested {quantity}, {sum(r.available for r in locked)} available "
                "across the allowed warehouses."
            )

        if tracks_serial:
            in_stock_counts = dict(
                SerialUnit.objects.filter(variant=variant, status="in_stock")
                .values("warehouse_id")
                .annotate(count=Count("id"))
                .values_list("warehouse_id", "count")
            )
            eligible = [
                row
                for row in eligible
                if in_stock_counts.get(row.warehouse_id, 0) >= quantity
            ]
            if not eligible:
                raise ValidationError(
                    f"Insufficient in-stock serial units for variant {variant.pk} "
                    "across the allowed warehouses."
                )

        if warehouse_ids is not None:
            priority = {pk: index for index, pk in enumerate(warehouse_ids)}
            chosen = min(
                eligible,
                key=lambda row: priority.get(row.warehouse_id, len(warehouse_ids)),
            )
        else:
            chosen = max(eligible, key=lambda row: row.available)

        units = None
        if tracks_serial:
            units = list(
                SerialUnit.objects.select_for_update()
                .filter(
                    variant=variant,
                    warehouse=chosen.warehouse,
                    status="in_stock",
                )
                .order_by("received_at", "pk")[:quantity]
            )
            if len(units) < quantity:
                raise ValidationError(
                    f"Insufficient in-stock serial units for variant {variant.pk}."
                )

        reserved_row = chosen
        reserved_row.reserved = F("reserved") + quantity
        reserved_row.save(update_fields=["reserved", "updated_at"])

        reservation = StockReservation.objects.create(
            inventory=chosen,
            quantity=quantity,
            expires_at=expires_at,
        )
        if units:
            SerialUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(
                status="reserved", reservation=reservation
            )
        return reservation


def _load_reservation_for_update(reservation):
    """Return a locked reservation with its inventory row.

    Args:
        reservation (StockReservation | int): the reservation or its pk.

    Returns:
        StockReservation: a ``select_for_update`` row with ``inventory``
            selected and serial-unit related rows eager-loaded.

    Raises:
        StockReservation.DoesNotExist: if the reservation is missing.
    """
    return (
        StockReservation.objects.select_for_update()
        .select_related("inventory__warehouse")
        .get(
            pk=(
                reservation.pk
                if isinstance(reservation, StockReservation)
                else reservation
            )
        )
    )


def fulfill_reservation(reservation, user=None):
    """Realize a reservation: deduct stock and mark its serial units sold.

    Idempotent: a reservation that is already fulfilled or released returns
    ``False`` and no stock moves. A released reservation (for example one the
    expiry sweep freed while the order was still pending) therefore cannot
    quietly confirm — callers must re-check availability and, if needed,
    re-reserve before treating the order as confirmed. For serialized
    variants the units owned by this reservation are marked ``sold`` in the
    same transaction as the deduction.

    Args:
        reservation (StockReservation): the reservation to fulfill.
        user (User | None): unused (the reservation row captures fulfilment).

    Returns:
        bool: True when the reservation was fulfilled, False when it was
            already in a terminal state.

    Raises:
        ValidationError: if the reservation's reserved serial units are
            missing for a serialized variant.
    """
    with transaction.atomic():
        locked = _load_reservation_for_update(reservation)
        if locked.status != "active":
            return False

        variant_id = locked.inventory.variant_id
        inventory = Inventory.objects.select_for_update().get(pk=locked.inventory_id)
        if _variant_tracks_serial_numbers(variant_id):
            units = list(
                SerialUnit.objects.select_for_update()
                .filter(
                    variant_id=variant_id,
                    warehouse_id=inventory.warehouse_id,
                    status="reserved",
                    reservation=locked,
                )
                .order_by("received_at", "pk")[: locked.quantity]
            )
            if len(units) < locked.quantity:
                raise ValidationError(
                    f"Insufficient reserved serial units to fulfill reservation "
                    f"{locked.pk}."
                )
            SerialUnit.objects.filter(pk__in=[unit.pk for unit in units]).update(
                status="sold", reservation=None
            )

        Inventory.objects.filter(pk=inventory.pk).update(
            quantity=F("quantity") - locked.quantity,
            reserved=F("reserved") - locked.quantity,
        )
        locked.status = "fulfilled"
        locked.save(update_fields=["status"])
    return True


def release_reservation(reservation, user=None):
    """Return a reservation's stock to available.

    Idempotent: a reservation that is already fulfilled or released returns
    ``False`` and no stock moves. Only the reservation's own reserved serial
    units return to ``in_stock`` (ownership is per-reservation), and the
    reserved-count decrease happens in the same transaction.

    Args:
        reservation (StockReservation): the reservation to release.
        user (User | None): the acting user, recorded in the movement log.

    Returns:
        bool: True when the reservation was released, False when it was
            already in a terminal state.
    """
    with transaction.atomic():
        locked = _load_reservation_for_update(reservation)
        if locked.status != "active":
            return False

        variant_id = locked.inventory.variant_id
        inventory = Inventory.objects.select_for_update().get(pk=locked.inventory_id)
        if _variant_tracks_serial_numbers(variant_id):
            SerialUnit.objects.filter(
                variant_id=variant_id,
                warehouse_id=inventory.warehouse_id,
                status="reserved",
                reservation=locked,
            ).update(status="in_stock", reservation=None)

        Inventory.objects.filter(pk=inventory.pk).update(
            reserved=F("reserved") - locked.quantity
        )
        locked.status = "released"
        locked.released_at = timezone.now()
        locked.save(update_fields=["status", "released_at"])
        _log_movement(
            user=user,
            action="release",
            inventory=inventory,
            reserved_change=-locked.quantity,
        )
    return True


def update_serial_unit_status(unit, new_status, user=None):
    """Move a serial unit between manually-owned states, reconciling counts.

    Allowed transitions: ``in_stock`` -> ``returned``/``defective``, and
    ``returned`` -> ``in_stock``/``defective``. Units that are ``reserved``
    or ``sold`` are owned by the reservation/returns lifecycle and cannot be
    changed directly; ``defective`` is terminal so a withdrawn unit cannot
    silently re-enter sellable stock. Moving a unit out of ``in_stock``
    reduces the count ledger's sellable units by one, and moving it back to
    ``in_stock`` restores it.

    Args:
        unit (SerialUnit): the unit to transition.
        new_status (str): the target status.
        user (User | None): the acting user, recorded in the movement log.

    Returns:
        SerialUnit: the updated unit.

    Raises:
        ValidationError: on a disallowed transition, a missing warehouse, or
            a missing ledger row for reconciliation.
    """
    allowed_targets = _SERIAL_TRANSITIONS.get(unit.status, set())
    if new_status not in allowed_targets:
        raise ValidationError(
            f"Cannot move a '{unit.status}' serial unit to '{new_status}'. "
            "Reserved and sold units are owned by the reservation and "
            "returns lifecycle; defective units are terminal."
        )
    if unit.warehouse_id is None:
        raise ValidationError(
            "This serial unit has no warehouse; it cannot be moved between "
            "sellable and non-sellable states."
        )
    if unit.status == new_status:
        return unit

    old_status = unit.status
    inventory = (
        Inventory.objects.select_for_update()
        .filter(variant=unit.variant, warehouse=unit.warehouse)
        .first()
    )
    if inventory is None:
        raise ValidationError(
            "No inventory ledger row exists for this serial unit; reconcile "
            "stock before changing its state."
        )

    quantity_change = 0
    if unit.status == "in_stock":
        quantity_change = -1
    elif unit.status == "returned" and new_status == "in_stock":
        quantity_change = 1

    with transaction.atomic():
        if quantity_change:
            inventory.quantity = F("quantity") + quantity_change
            inventory.save(update_fields=["quantity", "updated_at"])
        unit.status = new_status
        unit.save(update_fields=["status"])
        _log_movement(
            user=user,
            action="serial_status",
            inventory=inventory,
            serial_number=unit.serial_number,
            from_status=old_status,
            to_status=new_status,
        )
    return unit
