"""API views for the dashboard app.

Every endpoint is a staff widget gated by the manager/analyst role — the only
roles that may see revenue and internal aggregates. Like the analytics app
these are pure read views: no create/update exists, every payload is a
computed dict (never a writable model row), and each view resolves its query
string through an explicit serializer before touching the database. The
current-state widgets (COD, stock, collections, warehouse routing, alerts)
accept no query parameters at all.
"""

from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrAnalyst
from apps.dashboard.selectors import (
    alerts,
    bundles_dashboard,
    cod_dashboard,
    collections_dashboard,
    products_dashboard,
    promotions_dashboard,
    returns_dashboard,
    sales_dashboard,
    stock_dashboard,
    support_dashboard,
    warehouse_routing_dashboard,
)
from apps.dashboard.serializers import (
    DashboardQuerySerializer,
    ProductsQuerySerializer,
)


class NoParamsSerializer(serializers.Serializer):
    """Serializer declaring no query parameters.

    Used by the current-state widgets so any query string, including
    ``from``/``to``, is rejected rather than silently ignored.
    """


class DashboardAPIView(APIView):
    """Base view for all dashboard widgets.

    Grants manager/analyst-only access, throttles dashboard traffic under the
    ``dashboard_read`` scope, and exposes ``validated_params`` so a subclass
    can resolve its query string through a declared serializer.
    """

    permission_classes = [IsManagerOrAnalyst]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "dashboard_read"
    query_serializer_class = DashboardQuerySerializer

    def validated_params(self):
        """Return the validated query parameters for this request.

        Allowed parameters are derived from the declared serializer fields,
        with ``start``/``end`` exposed on the wire as ``from``/``to`` (``from``
        is a reserved word). Every other query parameter is rejected. A bound
        is included only when present so an omitted one stays absent rather
        than arriving as an explicit null.

        Returns:
            dict: the validated query values.
        """
        query_params = self.request.query_params
        serializer_class = self.query_serializer_class
        wire_names = {"start": "from", "end": "to"}
        declared = serializer_class().fields
        allowed_params = {wire_names.get(name, name) for name in declared}
        unknown = set(query_params) - allowed_params
        if unknown:
            raise serializers.ValidationError(
                {"detail": f"Unknown query parameter(s): {', '.join(sorted(unknown))}."}
            )
        query_data = {}
        for name in declared:
            wire = wire_names.get(name, name)
            if wire in query_params:
                query_data[name] = query_params.get(wire)
        serializer = serializer_class(data=query_data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class SalesDashboardView(DashboardAPIView):
    """Return the sales-overview widget for the period."""

    def get(self, request):
        """Return the sales widget.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the sales widget payload.
        """
        params = self.validated_params()
        return Response(sales_dashboard(params.get("start"), params.get("end")))


class CodOperationsDashboardView(DashboardAPIView):
    """Return the COD-operations widget for the current state."""

    query_serializer_class = NoParamsSerializer

    def get(self, request):
        """Return the COD-operations widget.

        Args:
            request: the GET request (no parameters accepted).

        Returns:
            Response: the COD-operations widget payload.
        """
        self.validated_params()
        return Response(cod_dashboard())


class StockDashboardView(DashboardAPIView):
    """Return the stock-and-reservations widget for the current state."""

    query_serializer_class = NoParamsSerializer

    def get(self, request):
        """Return the stock widget.

        Args:
            request: the GET request (no parameters accepted).

        Returns:
            Response: the stock-and-reservations widget payload.
        """
        self.validated_params()
        return Response(stock_dashboard())


class ProductsDashboardView(DashboardAPIView):
    """Return the product-performance widget for the period."""

    query_serializer_class = ProductsQuerySerializer

    def get(self, request):
        """Return the products widget.

        Args:
            request: the GET request with optional ``from``/``to`` and
                ``limit`` capping how many top products are returned.

        Returns:
            Response: the products widget payload.
        """
        params = self.validated_params()
        return Response(
            products_dashboard(
                params.get("start"),
                params.get("end"),
                limit=params.get("limit", 10),
            )
        )


class CollectionsDashboardView(DashboardAPIView):
    """Return the smart-collection refresh-status widget."""

    query_serializer_class = NoParamsSerializer

    def get(self, request):
        """Return the collections widget.

        Args:
            request: the GET request (no parameters accepted).

        Returns:
            Response: the collections widget payload.
        """
        self.validated_params()
        return Response(collections_dashboard())


class BundlesDashboardView(DashboardAPIView):
    """Return the bundle-performance widget for the period."""

    def get(self, request):
        """Return the bundles widget.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the bundles widget payload.
        """
        params = self.validated_params()
        return Response(bundles_dashboard(params.get("start"), params.get("end")))


class PromotionsDashboardView(DashboardAPIView):
    """Return the promotions widget for the period."""

    def get(self, request):
        """Return the promotions widget.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the promotions widget payload.
        """
        params = self.validated_params()
        return Response(promotions_dashboard(params.get("start"), params.get("end")))


class ReturnsDashboardView(DashboardAPIView):
    """Return the returns/RMA widget for the period."""

    def get(self, request):
        """Return the returns widget.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the returns widget payload.
        """
        params = self.validated_params()
        return Response(returns_dashboard(params.get("start"), params.get("end")))


class WarehouseRoutingDashboardView(DashboardAPIView):
    """Return the warehouse-routing coverage widget."""

    query_serializer_class = NoParamsSerializer

    def get(self, request):
        """Return the warehouse-routing widget.

        Args:
            request: the GET request (no parameters accepted).

        Returns:
            Response: the warehouse-routing widget payload.
        """
        self.validated_params()
        return Response(warehouse_routing_dashboard())


class SupportDashboardView(DashboardAPIView):
    """Return the support widget for the period."""

    def get(self, request):
        """Return the support widget.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the support widget payload.
        """
        params = self.validated_params()
        return Response(support_dashboard(params.get("start"), params.get("end")))


class AlertsDashboardView(DashboardAPIView):
    """Return the live alert feed, computed fresh on every call."""

    query_serializer_class = NoParamsSerializer

    def get(self, request):
        """Return the current alert feed.

        Args:
            request: the GET request (no parameters accepted).

        Returns:
            Response: the alert feed payload.
        """
        self.validated_params()
        return Response(alerts())
