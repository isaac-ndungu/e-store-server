"""Data models for the shipping app.

Holds the delivery-zone catalogue (``DeliveryZone``) and the per-zone
warehouse routing ranking (``WarehouseZonePriority``). Delivery zones price
shipping by billable weight and may waive the fee above a free-shipping
subtotal; the priority rows are the computable rule that picks which
warehouse fulfils an order destined for a given zone.
"""

from django.db import models
from django.db.models import Q
from django.db.models.constraints import CheckConstraint


class DeliveryZone(models.Model):
    """A delivery area with its own fee schedule and courier.

    ``county`` + ``area_name`` locate the zone; ``county`` is validated
    against the 47 Kenyan counties (see ``apps.shipping.constants``) and a
    county + area names exactly one zone, so a checkout cannot route to two
    equally-plausible prices. ``base_fee`` and ``per_kg_rate`` price the zone
    by billable weight (see ``apps.shipping.services.calculate_shipping_fee``)
    and are constrained non-negative, as money must be. When a cart's
    subtotal reaches ``free_shipping_threshold`` the fee is waived;
    ``is_active`` controls whether the zone is offered to the storefront.
    """

    county = models.CharField(max_length=100)
    area_name = models.CharField(max_length=255)
    base_fee = models.DecimalField(max_digits=10, decimal_places=2)
    per_kg_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    free_shipping_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    estimated_days = models.PositiveIntegerField(default=2)
    courier_partner = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["county", "area_name"]
        unique_together = [("county", "area_name")]
        indexes = [
            models.Index(fields=["is_active"], name="zone_active_idx"),
            models.Index(fields=["county"], name="zone_county_idx"),
        ]
        constraints = [
            CheckConstraint(condition=Q(base_fee__gte=0), name="dz_base_fee_gte_0"),
            CheckConstraint(
                condition=Q(per_kg_rate__gte=0), name="dz_per_kg_rate_gte_0"
            ),
            CheckConstraint(
                condition=Q(free_shipping_threshold__isnull=True)
                | Q(free_shipping_threshold__gt=0),
                name="dz_threshold_null_or_positive",
            ),
        ]

    def __str__(self):
        """Return a short human-readable zone label."""
        return f"{self.area_name}, {self.county}"


class WarehouseZonePriority(models.Model):
    """Ranking of the preferred fulfilment warehouses for a delivery zone.

    ``delivery_zone`` selects the zone and ``warehouse`` the location; lower
    ``priority`` values are tried first when picking where an order's stock is
    reserved from. Routing falls back to any active warehouse with enough
    stock when none of the ranked options can cover the quantity. ``warehouse``
    is a plain ``ForeignKey`` (not a one-to-one) to ``inventory.Warehouse``:
    a warehouse may serve many zones.

    A zone never lists the same warehouse twice (``delivery_zone,
    warehouse``) and never assigns the same rank twice (``delivery_zone,
    priority``), so the routing order is a total order — there is no
    tie-breaker to guess. ``priority`` numbers only have meaning relative to
    other rows for the same zone; gaps are fine.
    """

    delivery_zone = models.ForeignKey(
        DeliveryZone,
        related_name="warehouse_priorities",
        on_delete=models.CASCADE,
    )
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.CASCADE)
    priority = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [
            ("delivery_zone", "warehouse"),
            ("delivery_zone", "priority"),
        ]
        ordering = ["priority", "id"]
        indexes = [
            models.Index(fields=["warehouse"], name="wzp_warehouse_idx"),
        ]

    def __str__(self):
        """Return a compact label identifying zone and warehouse rank."""
        return (
            f"{self.delivery_zone} -> {self.warehouse.name} (priority {self.priority})"
        )
