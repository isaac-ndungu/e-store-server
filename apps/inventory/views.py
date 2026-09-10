"""API views for the inventory app.

Split into public availability endpoints (``AllowAny``) and admin CRUD views
(``IsAdminUser``). Views stay thin: parse input, call a service or selector,
return a response. Stock-intake create endpoints enforce an
``Idempotency-Key`` so a retried scan or receipt cannot double-apply.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import filters, generics, permissions
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.catalog.models import ProductVariant
from apps.core.idempotency import IdempotentCreateMixin
from apps.inventory.models import Inventory, SerialUnit, StockReservation, Warehouse
from apps.inventory.selectors import (
    get_variant_availability,
    list_inventory,
    list_reservations,
    list_serial_units,
)
from apps.inventory.serializers import (
    BulkAvailabilitySerializer,
    InventorySerializer,
    InventoryWriteSerializer,
    SerialUnitSerializer,
    SerialUnitWriteSerializer,
    StockReservationSerializer,
    WarehouseSerializer,
)
from apps.inventory.services import release_reservation


def _is_sellable_variant(variant):
    """Return whether a variant is currently sold through the storefront.

    Stock availability is only exposed for active, non-discontinued products;
    inactive or discontinued lines must not leak stock levels.

    Args:
        variant (ProductVariant): the variant to check.

    Returns:
        bool: True when the variant's product is active and not discontinued.
    """
    product = variant.product
    return product.is_active and not product.is_discontinued


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

    @extend_schema(
        operation_id="variant_availability",
        responses={200: dict},
    )
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
        if not _is_sellable_variant(variant):
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

    @extend_schema(
        operation_id="variant_availability_bulk",
        request=BulkAvailabilitySerializer,
        responses={200: dict},
    )
    def post(self, request):
        """Return a map of variant id to availability.

        Args:
            request: the POST request.

        Returns:
            Response: ``{"availability": {<variant id>: {...}}}``.

        Raises:
            ValidationError: on an empty/oversized/invalid request body or a
                missing or non-sellable variant/sku.
        """
        input_serializer = BulkAvailabilitySerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        lookup = input_serializer.validated_data
        ids = lookup.get("variant_ids") or []
        sku_list = lookup.get("skus") or []

        if ids:
            variants = list(
                ProductVariant.objects.filter(pk__in=ids).select_related("product")
            )
            missing = set(ids) - {variant.pk for variant in variants}
            if missing:
                raise DRFValidationError(
                    {"variant_ids": f"Unknown variant ids: {sorted(missing)}"}
                )
        else:
            variants = list(
                ProductVariant.objects.filter(sku__in=sku_list).select_related(
                    "product"
                )
            )
            missing = set(sku_list) - {variant.sku for variant in variants}
            if missing:
                raise DRFValidationError({"skus": f"Unknown skus: {sorted(missing)}"})

        non_sellable = [
            variant for variant in variants if not _is_sellable_variant(variant)
        ]
        if non_sellable:
            raise DRFValidationError(
                {
                    "detail": (
                        "Stock is not exposed for inactive or discontinued " "products."
                    )
                }
            )

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
    """Retrieve, update, or delete a warehouse (admin only).

    Deletion is rejected while the warehouse still holds inventory or serial
    units — deleting the row would cascade away real stock and its
    reservations instead of moving it through the lifecycle.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inventory_write"
    serializer_class = WarehouseSerializer
    queryset = Warehouse.objects.all()

    def destroy(self, request, *args, **kwargs):
        """Delete the warehouse unless it still holds stock.

        Args:
            request: the DELETE request.

        Returns:
            Response: ``204 No Content`` on safe deletion, or ``400`` when the
                warehouse cannot be emptied out.
        """
        warehouse = self.get_object()
        holdings = Inventory.objects.filter(warehouse=warehouse).exists()
        serial_units = SerialUnit.objects.filter(warehouse=warehouse).exists()
        if holdings or serial_units:
            raise DRFValidationError(
                {
                    "detail": (
                        "This warehouse still holds inventory or serial "
                        "units; move or clear them before deleting it."
                    )
                }
            )
        return super().destroy(request, *args, **kwargs)


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
    Deletion is rejected while the row backs any reservation or serialized
    unit — dropping the row would silently destroy the reservation lifecycle
    and leave the serial-unit ledger unreconciled.
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

    def destroy(self, request, *args, **kwargs):
        """Delete the row unless stock or reservations back it.

        Args:
            request: the DELETE request.

        Returns:
            Response: ``204 No Content`` on safe deletion, or ``400`` when
                the row is still in use.
        """
        row = self.get_object()
        in_use = StockReservation.objects.filter(inventory=row).exists()
        has_units = SerialUnit.objects.filter(
            variant=row.variant, warehouse=row.warehouse
        ).exists()
        if in_use or has_units:
            raise DRFValidationError(
                {
                    "detail": (
                        "This inventory row holds stock or backs open "
                        "reservations; move stock through the reservation "
                        "lifecycle instead of deleting it."
                    )
                }
            )
        return super().destroy(request, *args, **kwargs)


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
    serializer. Deletion is allowed only for a plain ``in_stock`` unit — a
    reserved, sold, returned, or delivery-linked unit must move through the
    reservation/returns lifecycle so the sibling inventory counts never
    drift.
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

    def destroy(self, request, *args, **kwargs):
        """Delete only an unattached, in-stock serial unit.

        Args:
            request: the DELETE request.

        Returns:
            Response: ``204 No Content`` on safe deletion, or ``400`` when
                the unit is linked to the stock lifecycle.
        """
        unit = self.get_object()
        if (
            unit.status != "in_stock"
            or unit.reservation_id is not None
            or unit.order_item_id is not None
        ):
            raise DRFValidationError(
                {
                    "detail": (
                        "Only an in-stock unit with no reservation or order "
                        "link can be deleted; other units must move through "
                        "the reservation lifecycle."
                    )
                }
            )
        return super().destroy(request, *args, **kwargs)


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
    schema = None

    @extend_schema(
        operation_id="admin_reservation_release",
        responses={200: StockReservationSerializer},
    )
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
