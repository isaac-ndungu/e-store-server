"""API views for the shipping app.

Split into a public endpoint (``AllowAny``: the delivery-area catalogue for
the storefront picker) and admin CRUD views (``IsAdminUser``) for the area
list. There is no quote endpoint — delivery cost is confirmed with the
customer by staff and typed into the order at intake. Views stay thin:
parse input, call a selector, return a response.
"""

from rest_framework import generics, permissions
from rest_framework.throttling import ScopedRateThrottle

from apps.shipping.models import DeliveryArea
from apps.shipping.selectors import list_delivery_areas
from apps.shipping.serializers import DeliveryAreaSerializer

# Public views


class DeliveryAreaListView(generics.ListAPIView):
    """List the delivery areas offered to the storefront (public).

    Only active areas are returned. Supports an optional ``county`` query
    filter. Each row answers "do we deliver here" — no pricing attached.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = DeliveryAreaSerializer

    def get_queryset(self):
        """Return active areas, optionally narrowed to one county."""
        return list_delivery_areas(
            active_only=True, county=self.request.query_params.get("county")
        )


# Admin views


class AdminDeliveryAreaListCreateView(generics.ListCreateAPIView):
    """List all delivery areas or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = DeliveryAreaSerializer

    def get_queryset(self):
        """Return all areas (active and inactive) for admin management."""
        return list_delivery_areas(county=self.request.query_params.get("county"))


class AdminDeliveryAreaDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a delivery area (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = DeliveryAreaSerializer
    queryset = DeliveryArea.objects.all()
