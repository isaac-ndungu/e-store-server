"""Data models for the shipping app.

``DeliveryArea`` is the staff-maintained list of places the store serves —
nothing more. It carries no pricing: delivery cost is quoted by staff in the
sales conversation and typed into the order at intake. The county + area
pair keeps the storefront picker consistent so orders cannot come from
places that are not served.
"""

from django.db import models


class DeliveryArea(models.Model):
    """A county + area pair the store delivers to.

    ``county`` is validated against the 47 Kenyan counties at write time
    (see the serializers); a county + area names exactly one area, so a
    checkout cannot route to two equally-plausible entries. ``is_active``
    controls whether the area is offered to the storefront.
    """

    county = models.CharField(max_length=100)
    area_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["county", "area_name"]
        unique_together = [("county", "area_name")]
        indexes = [
            models.Index(fields=["is_active"], name="zone_active_idx"),
            models.Index(fields=["county"], name="zone_county_idx"),
        ]

    def __str__(self):
        """Return a short human-readable area label."""
        return f"{self.area_name}, {self.county}"
