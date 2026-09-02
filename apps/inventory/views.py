"""API views for the inventory app.

Split into public availability endpoints (``AllowAny``) and admin CRUD views
(``IsAdminUser``). Views stay thin: parse input, call a service or selector,
return a response. Stock-intake create endpoints enforce an
``Idempotency-Key`` so a retried scan or receipt cannot double-apply.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import filters, generics, permissions
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.catalog.models import ProductVariant
from apps.core.idempotency import IdempotentCreateMixin
from apps.inventory.models import StockReservation, Warehouse
from apps.inventory.selectors import (
    get_variant_availability,
    list_inventory,
    list_reservations,
    list_serial_units,
)
from apps.inventory.serializers import (
    InventorySerializer,
    InventoryWriteSerializer,
    SerialUnitSerializer,
    SerialUnitWriteSerializer,
    StockReservationSerializer,
    WarehouseSerializer,
)
from apps.inventory.services import release_reservation

# Biggest batch an availability request may ask for in one call.
MAX_BULK_AVAILABILITY = 50


# Public views


class AvailabilityView(APIView):
    """Return per-warehouse and aggregate availability for a variant.

    Public: the storefront uses this to render stock badges, the "almost
    gone" state, and low-stock signals. Accepts the variant primary key or
    SKU as a query parameter. Products that are inactive or discontinued do
    not expose stock, so the response is 404.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request):
        """Return availability for the requested variant, or 404.

        Args:
            request: the GET request.

        Returns:
            Response: availability data keyed by variant, or 404.
        """
        variant_id = request.query_params.get("variant")
        sku = request.query_params.get("sku")
        variant = None
        if variant_id:
            variant = get_object_or_404(ProductVariant, pk=variant_id)
        elif sku:
            variant = get_object_or_404(ProductVariant, sku=sku)
        else:
            raise DRFValidationError(
                {"variant": "Provide a 'variant' id or 'sku' query parameter."}
            )
        self._ensure_sellable(variant)
        availability = get_variant_availability(variant)
        availability["tracks_serial_numbers"] = variant.product.tracks_serial_numbers
        return Response(availability)

    @staticmethod
    def _ensure_sellable(variant):
        """Raise 404 for products not sold through the storefront.

        Args:
            variant (ProductVariant): the resolved variant.

        Raises:
            Http404: if the product is inactive or discontinued.
        """
        product = variant.product
        if not product.is_active or product.is_discontinued:
            raise Http404


class BulkAvailabilityView(APIView):
    """Return availability for a batch of variants in one request.

    Public: the cart and product-list screens fetch stock for many lines at
    once, which would otherwise mean one request per variant over a throttled
    connection. Accepts ``variant_ids`` or ``skus`` (not both) in the JSON
    body. Every requested variant must exist; unknown lookups fail the whole
    request so the caller can reconcile its catalog.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def post(self, request):
        """Return a map of variant id to availability.

        Args:
            request: the POST request.

        Returns:
            Response: ``{"availability": {<variant id>: {...}}}``.

        Raises:
            ValidationError: on an empty/oversized/invalid request body or a
                missing variant/sku.
        """
        variant_ids = request.data.get("variant_ids")
        skus = request.data.get("skus")
        if bool(variant_ids) == bool(skus):
            raise DRFValidationError(
                "Provide exactly one of 'variant_ids' or 'skus' (non-empty)."
            )
        ids = list(variant_ids or [])
        sku_list = list(skus or [])
        if len(ids) > MAX_BULK_AVAILABILITY or len(sku_list) > MAX_BULK_AVAILABILITY:
            raise DRFValidationError(
                f"At most {MAX_BULK_AVAILABILITY} lookups per request."
            )
        if variant_ids:
            variants = list(ProductVariant.objects.filter(pk__in=ids))
            missing = set(ids) - {variant.pk for variant in variants}
            if missing:
                raise DRFValidationError(
                    {"variant_ids": f"Unknown variant ids: {sorted(missing)}"}
                )
        else:
            variants = list(ProductVariant.objects.filter(sku__in=sku_list))
            missing = set(sku_list) - {variant.sku for variant in variants}
            if missing:
                raise DRFValidationError({"skus": f"Unknown skus: {sorted(missing)}"})

        results = {}
        for variant in variants:
            availability = get_variant_availability(variant)
            availability["tracks_serial_numbers"] = (
                variant.product.tracks_serial_numbers
            )
            results[variant.pk] = availability
        return Response({"availability": results})


# Admin views


class AdminWarehouseListCreateView(generics.ListCreateAPIView):
    """List all warehouses or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.all().order_by("name")


class AdminWarehouseDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a warehouse (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.all()


class AdminInventoryListCreateView(IdempotentCreateMixin, generics.ListCreateAPIView):
    """List inventory rows or stock a variant in a warehouse (admin only).

    Supports ``variant`` and ``warehouse`` query filters. Stock intake
    enforces an ``Idempotency-Key`` header so a retried receipt does not
    double-apply.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"

    def get_serializer_class(self):
        """Return the read serializer for GET and the write one otherwise."""
        if self.request.method == "GET":
            return InventorySerializer
        return InventoryWriteSerializer

    def get_queryset(self):
        """Return inventory rows with query-param filters applied."""
        return list_inventory(
            variant_id=self.request.query_params.get("variant"),
            warehouse_id=self.request.query_params.get("warehouse"),
        )


class AdminInventoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete an inventory row (admin only).

    Updates may only increase stock; see ``InventoryWriteSerializer``.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"

    def get_serializer_class(self):
        """Return the read serializer for GET and the write one otherwise."""
        if self.request.method == "GET":
            return InventorySerializer
        return InventoryWriteSerializer

    def get_queryset(self):
        """Return inventory rows with related data pre-fetched."""
        return list_inventory()


class AdminSerialUnitListCreateView(IdempotentCreateMixin, generics.ListCreateAPIView):
    """List serial units or register a new one (admin only).

    Supports ``variant``, ``warehouse``, and ``status`` query filters plus
    free-text search across the serial number and variant SKU/product name.
    Registration enforces an ``Idempotency-Key`` header.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"
    filter_backends = [filters.SearchFilter]
    search_fields = [
        "serial_number",
        "variant__sku",
        "variant__product__name",
    ]

    def get_serializer_class(self):
        """Return the read serializer for GET and the write one otherwise."""
        if self.request.method == "GET":
            return SerialUnitSerializer
        return SerialUnitWriteSerializer

    def get_queryset(self):
        """Return serial units with query-param filters applied."""
        return list_serial_units(
            variant_id=self.request.query_params.get("variant"),
            warehouse_id=self.request.query_params.get("warehouse"),
            status=self.request.query_params.get("status"),
        )


class AdminSerialUnitDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a serial unit (admin only).

    Updates only allow the manual status transitions enforced by the write
    serializer.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"

    def get_serializer_class(self):
        """Return the read serializer for GET and the write one otherwise."""
        if self.request.method == "GET":
            return SerialUnitSerializer
        return SerialUnitWriteSerializer

    def get_queryset(self):
        """Return serial units with related data pre-fetched."""
        return list_serial_units()


class AdminReservationListView(generics.ListAPIView):
    """List stock reservations (admin only).

    Supports a ``status`` query filter.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = StockReservationSerializer

    def get_queryset(self):
        """Return reservations, optionally filtered by status."""
        return list_reservations(status=self.request.query_params.get("status"))


class AdminReservationDetailView(generics.RetrieveAPIView):
    """Retrieve a single stock reservation (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = StockReservationSerializer

    def get_queryset(self):
        """Return reservations with related data pre-fetched."""
        return list_reservations()


class AdminReservationReleaseView(APIView):
    """Release an active stock reservation (admin only).

    Idempotent: releasing an already-released or fulfilled reservation is a
    no-op returning the current state.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"

    def post(self, request, pk):
        """Release the reservation and return its updated state.

        Args:
            request: the POST request.
            pk (int): the reservation primary key.

        Returns:
            Response: the serialized reservation after release.
        """
        reservation = get_object_or_404(StockReservation, pk=pk)
        try:
            release_reservation(reservation, user=request.user)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.messages) from exc
        reservation.refresh_from_db()
        return Response(StockReservationSerializer(reservation).data)
