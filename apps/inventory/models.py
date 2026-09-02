

from django.db import models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint


class Warehouse(models.Model):
    """A physical location holding stock.

    Warehouses are the subject of per-warehouse inventory and are ranked per
    delivery zone when picking a fulfillment source. ``is_active`` marks a
    location that still physically exists but must not receive new stock or
    be considered for availability/reservation (decommissioned or mothballed).
    """

    name = models.CharField(max_length=255)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"], name="wh_name_idx"),
        ]

    def __str__(self):
        """Return the warehouse name."""
        return self.name


class Inventory(models.Model):
    """Count-based stock for one variant in one warehouse.

    ``variant`` is a ``ForeignKey`` (not ``OneToOneField``): a variant can be
    stocked in multiple warehouses, each with its own row. ``reserved`` counts
    units held by active ``StockReservation`` rows; ``available`` is
    ``quantity - reserved``. A database check keeps ``reserved`` from ever
    exceeding ``quantity``, so an oversold state is rejected at the storage
    layer rather than relied on in application code.

    Counts are only ever decreased by the reservation lifecycle — direct
    decrements are rejected by the service layer.
    """

    variant = models.ForeignKey(
        "catalog.ProductVariant",
        related_name="inventory_records",
        on_delete=models.CASCADE,
    )
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    low_stock_threshold = models.PositiveIntegerField(default=5)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("variant", "warehouse")
        indexes = [
            models.Index(fields=["variant"], name="inv_variant_idx"),
            models.Index(fields=["warehouse"], name="inv_wh_idx"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(reserved__lte=models.F("quantity")),
                name="inv_reserved_lte_quantity",
            ),
            CheckConstraint(
                condition=Q(quantity__gte=0),
                name="inv_quantity_gte_0",
            ),
            CheckConstraint(
                condition=Q(reserved__gte=0),
                name="inv_reserved_gte_0",
            ),
        ]

    def __str__(self):
        """Return a compact label identifying variant, warehouse, and count."""
        return f"{self.variant_id} @ {self.warehouse.name} ({self.quantity - self.reserved} available)"

    @property
    def available(self):
        """Return the number of units not currently reserved."""
        return self.quantity - self.reserved

    @property
    def is_low_stock(self):
        """Return whether available units are at or below the reorder point."""
        return self.available <= self.low_stock_threshold


class SerialUnit(models.Model):
    """A single tracked unit of a serialized product.

    Exists for products where ``tracks_serial_numbers`` is set. ``status``
    tracks the unit through the stock lifecycle:

    - ``in_stock`` — physically available in ``warehouse``.
    - ``reserved`` — held by ``reservation`` (an active ``StockReservation``).
    - ``sold`` — fulfilled for an order line.
    - ``returned`` / ``defective`` — non-sellable or returned states.

    ``reservation`` records which active hold currently owns the unit. It is
    set when a reservation is created, cleared when the unit is released back
    to ``in_stock`` or fulfilled into ``sold``, and cleared on reservation
    deletion (``SET_NULL``). This ownership link is what lets every
    reservation release or fulfil exactly its own units even when several
    reservations for the same variant overlap in time.

    The ``status`` and the sibling ``Inventory`` counts always move together:
    reserving marks units reserved and raises the reserved count, fulfilling
    marks them sold and drops both counts, releasing returns them to
    in-stock and drops only the reserved count. No code path updates one
    without the other.
    """

    STATUS_CHOICES = (
        ("in_stock", "In Stock"),
        ("reserved", "Reserved"),
        ("sold", "Sold"),
        ("returned", "Returned"),
        ("defective", "Defective"),
    )

    variant = models.ForeignKey(
        "catalog.ProductVariant", related_name="serial_units", on_delete=models.CASCADE
    )
    warehouse = models.ForeignKey(
        Warehouse, null=True, blank=True, on_delete=models.SET_NULL
    )
    serial_number = models.CharField(max_length=150, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="in_stock")
    reservation = models.ForeignKey(
        "StockReservation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="serial_units",
    )
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["received_at", "pk"]
        indexes = [
            models.Index(
                fields=["variant", "warehouse", "status"], name="serial_vw_s_idx"
            ),
            models.Index(fields=["status"], name="serial_status_idx"),
            models.Index(fields=["reservation"], name="serial_resv_idx"),
        ]

    def __str__(self):
        """Return the serial number and current status."""
        return f"{self.serial_number} ({self.status})"


class StockReservation(models.Model):
    """A quantity of units held aside for an in-flight order line.

    Created while an order is pending: it raises the linked ``Inventory``
    reserved count and, for serialized products, marks that many units
    ``reserved``. On confirmation it is fulfilled (real deduction plus serial
    assignment) inside the same transaction; on failure or expiry it is
    released. Release cycles a reservation through ``active`` -> ``released``
    or ``active`` -> ``fulfilled``; a released or fulfilled reservation is a
    terminal state and is never reactivated.
    """

    STATUS_CHOICES = (
        ("active", "Active"),
        ("released", "Released"),
        ("fulfilled", "Fulfilled"),
    )

    inventory = models.ForeignKey(Inventory, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    reserved_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "expires_at"], name="resv_status_expiry_idx"
            ),
            models.Index(fields=["inventory"], name="resv_inventory_idx"),
        ]

    def __str__(self):
        """Return a compact label with status and quantity."""
        return f"Reservation {self.pk} ({self.status}, qty {self.quantity})"


class StockMovementLog(models.Model):
    """An audit entry recording a stock-affecting operation.

    Covers operations that change what the platform believes it holds:
    count intake, serial-unit intake, reservation release, and manual
    serial-unit status changes. Reservation create/fulfil are excluded — the
    reservation row itself captures those transitions. ``user`` is nullable
    because the expiry sweep runs unauthenticated; ``quantity_change`` and
    ``reserved_change`` record signed deltas applied to the ledger, and
    ``serial_number``/``from_status``/``to_status`` describe a single-unit
    transition when relevant.
    """

    ACTION_CHOICES = (
        ("receive_count", "Receive Count"),
        ("receive_serial", "Receive Serial"),
        ("release", "Release"),
        ("serial_status", "Serial Status Change"),
    )

    user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_movement_logs",
    )
    inventory = models.ForeignKey(
        Inventory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="movement_logs",
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_movement_logs",
    )
    warehouse = models.ForeignKey(
        Warehouse,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="movement_logs",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    quantity_change = models.IntegerField(default=0)
    reserved_change = models.IntegerField(default=0)
    serial_number = models.CharField(max_length=150, blank=True)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"], name="sml_created_idx"),
            models.Index(fields=["inventory"], name="sml_inventory_idx"),
            models.Index(fields=["variant"], name="sml_variant_idx"),
        ]

    def __str__(self):
        """Return a compact label with action and delta."""
        return f"{self.action} ({self.quantity_change:+d}) at {self.created_at}"
