"""API views for the shipping app.

Split into public endpoints (``AllowAny``: delivery-zone catalogue and
shipping quote) and admin CRUD views (``IsAdminUser``) for zones and
per-zone warehouse routing priorities. Views stay thin: parse input, call a
service or selector, return a response.
"""

from collections import namedtuple
from decimal import Decimal

from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.models import SiteConfig
from apps.shipping.models import DeliveryZone, WarehouseZonePriority
from apps.shipping.selectors import list_delivery_zones, list_zone_priorities
from apps.shipping.serializers import (
    DeliveryZoneSerializer,
    ShippingQuoteSerializer,
    WarehouseZonePrioritySerializer,
)
from apps.shipping.services import billable_kg, calculate_shipping_fee

# Carrier of a validated quote line; matches the shape the shipping service
# consumes (``.variant`` and ``.quantity``) without depending on the cart app.
_QuoteLine = namedtuple("_QuoteLine", ["variant", "quantity"])


# Public views


class DeliveryZoneListView(generics.ListAPIView):
    """List the delivery zones offered to the storefront (public).

    Only active zones are returned. Supports an optional ``county`` query
    filter.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = DeliveryZoneSerializer

    def get_queryset(self):
        """Return active zones, optionally narrowed to one county."""
        return list_delivery_zones(
            active_only=True, county=self.request.query_params.get("county")
        )


class ShippingQuoteView(APIView):
    """Quote shipping for a mixed-weight set of lines to a delivery zone.

    Public: the checkout page calls this before showing a delivery fee. The
    quote recomputes weight and the free-shipping subtotal server-side from
    current variant data, so the returned fee is authoritative and a client
    cannot understate weight or overstate the subtotal.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def post(self, request):
        """Return the shipping fee for the quoted lines.

        The response carries the fee, the currency read from site settings,
        the delivery estimate, the total billable weight, and a per-line
        weight breakdown so the storefront can show why the fee is what it
        is. Total and per-line weights are derived server-side from each
        variant's package data, never from client-supplied values.

        Args:
            request: the POST request.

        Returns:
            Response: the fee, weights, estimate, and free-shipping flag.

        Raises:
            ValidationError: if the zone is inactive, a line references a
                variant that is unavailable or not being sold, or a variant
                has no package weight or dimensions to price against.
        """
        serializer = ShippingQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        zone = data["delivery_zone"]

        lines = [
            _QuoteLine(item["variant"], item["quantity"]) for item in data["items"]
        ]
        try:
            total_weight_kg = Decimal("0.00")
            line_details = []
            for item in lines:
                weight_kg = billable_kg(item.variant) * item.quantity
                total_weight_kg += weight_kg
                line_details.append(
                    {
                        "variant": item.variant.pk,
                        "sku": item.variant.sku,
                        "quantity": item.quantity,
                        "weight_kg": str(weight_kg),
                    }
                )
            fee = calculate_shipping_fee(zone, lines)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        currency = SiteConfig.load().settings.get("currency", "KES")
        free_shipping = fee == 0
        return Response(
            {
                "delivery_zone": zone.pk,
                "fee": str(fee),
                "currency": currency,
                "free_shipping": free_shipping,
                "estimated_days": zone.estimated_days,
                "total_weight_kg": str(total_weight_kg),
                "lines": line_details,
            }
        )


# Admin views


class AdminDeliveryZoneListCreateView(generics.ListCreateAPIView):
    """List all delivery zones or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = DeliveryZoneSerializer

    def get_queryset(self):
        """Return all zones (active and inactive) for admin management."""
        return list_delivery_zones(county=self.request.query_params.get("county"))


class AdminDeliveryZoneDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a delivery zone (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = DeliveryZoneSerializer
    queryset = DeliveryZone.objects.all()


class AdminZonePriorityListCreateView(generics.ListCreateAPIView):
    """List or create per-zone warehouse routing priorities (admin only).

    Supports an optional ``delivery_zone`` query filter.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = WarehouseZonePrioritySerializer

    def get_queryset(self):
        """Return priorities, optionally filtered to one zone."""
        return list_zone_priorities(
            delivery_zone_id=self.request.query_params.get("delivery_zone")
        )


class AdminZonePriorityDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a routing priority (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = WarehouseZonePrioritySerializer
    queryset = WarehouseZonePriority.objects.all()
