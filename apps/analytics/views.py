"""API views for the analytics app.

Every endpoint is a staff report gated by the manager/analyst role — the only
roles that may see revenue and internal aggregates. Reports are read-only;
there is no create/update, so no idempotency or ownership machinery applies
(the caller either has the role or does not, and there is no per-resource
object to own). Each view validates its query string through an explicit
serializer before touching the database, rejects unknown parameters, and
computes aggregates from the source tables via ``apps.analytics.selectors``.
"""

from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsManagerOrAnalyst
from apps.analytics.selectors import (
    notifications_summary,
    product_performance,
    promotions_summary,
    returns_summary,
    reviews_summary,
    sales_summary,
    sales_timeseries,
    stock_snapshot,
    summary,
    support_summary,
    traffic_summary,
)
from apps.analytics.serializers import (
    AnalyticsQuerySerializer,
    ProductPerformanceQuerySerializer,
    SalesQuerySerializer,
)


class AnalyticsAPIView(APIView):
    """Base view for all analytics reports.

    Grants manager/analyst-only access, throttles dashboard traffic under the
    ``analytics_read`` scope, and exposes ``validated_params`` so a subclass can
    resolve its query string through the shared serializer.
    """

    permission_classes = [IsManagerOrAnalyst]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics_read"
    query_serializer_class = AnalyticsQuerySerializer

    def validated_params(self):
        """Return the validated query parameters for this request.

        The wire parameters are mapped onto the serializer's field names —
        ``from``/``to`` onto ``start``/``end`` (``from`` is a reserved word),
        and any extra filter the subclass serializer declares (``group_by``,
        ``limit``) straight through. Unknown parameters are rejected by the
        serializer.

        Returns:
            dict: the validated query values.
        """
        query_params = self.request.query_params
        serializer = self.query_serializer_class()
        allowed_params = {"from", "to"} | set(
            name for name in serializer.fields if name not in ("start", "end")
        )
        unknown = set(query_params) - allowed_params
        if unknown:
            raise serializers.ValidationError(
                {"detail": f"Unknown query parameter(s): {', '.join(sorted(unknown))}."}
            )
        # Map wire names onto serializer fields: ``from``/``to`` onto
        # ``start``/``end`` (``from`` is a reserved word), extras straight
        # through. A bound is included only when present so an omitted one
        # stays absent rather than arriving as an explicit null.
        query_data = {}
        if "from" in query_params:
            query_data["start"] = query_params.get("from")
        if "to" in query_params:
            query_data["end"] = query_params.get("to")
        for name in serializer.fields:
            if name not in ("start", "end") and name in query_params:
                query_data[name] = query_params.get(name)
        serializer = self.query_serializer_class(data=query_data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class DashboardSummaryView(AnalyticsAPIView):
    """Return the full dashboard summary for the requested period."""

    def get(self, request):
        """Return the assembled summary.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the full dashboard summary.
        """
        params = self.validated_params()
        return Response(summary(params.get("start"), params.get("end")))


class SalesReportView(AnalyticsAPIView):
    """Return the period-filtered sales report."""

    query_serializer_class = SalesQuerySerializer

    def get(self, request):
        """Return sales aggregates and a bucketed trend.

        Args:
            request: the GET request with optional ``from``/``to`` and
                ``group_by`` (``day``, ``week``, or ``month``).

        Returns:
            Response: ``{"summary": {...}, "series": [...]}``.
        """
        params = self.validated_params()
        from_time = params.get("start")
        to_time = params.get("end")
        group_by = params.get("group_by", "day")
        return Response(
            {
                "summary": sales_summary(from_time, to_time),
                "series": sales_timeseries(from_time, to_time, group_by=group_by),
            }
        )


class StockReportView(AnalyticsAPIView):
    """Return the current stock-and-reservations snapshot."""

    def get(self, request):
        """Return the stock snapshot (a live snapshot, not period-bound).

        Args:
            request: the GET request.

        Returns:
            Response: the stock report.
        """
        return Response(stock_snapshot())


class ProductPerformanceReportView(AnalyticsAPIView):
    """Return the top products by revenue for the period."""

    query_serializer_class = ProductPerformanceQuerySerializer

    def get(self, request):
        """Return the top products by revenue.

        Args:
            request: the GET request with optional ``from``/``to`` and
                ``limit`` capping how many products are returned.

        Returns:
            Response: ``{"products": [...]}``.
        """
        params = self.validated_params()
        return Response(
            {
                "products": product_performance(
                    params.get("start"),
                    params.get("end"),
                    limit=params.get("limit", 10),
                )
            }
        )


class PromotionsReportView(AnalyticsAPIView):
    """Return promotion and coupon usage for the period."""

    def get(self, request):
        """Return the promotions report.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the promotions aggregate.
        """
        params = self.validated_params()
        return Response(promotions_summary(params.get("start"), params.get("end")))


class ReturnsReportView(AnalyticsAPIView):
    """Return return-request figures for the period."""

    def get(self, request):
        """Return the returns report.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the returns aggregate.
        """
        params = self.validated_params()
        return Response(returns_summary(params.get("start"), params.get("end")))


class SupportReportView(AnalyticsAPIView):
    """Return support-ticket figures for the period."""

    def get(self, request):
        """Return the support report.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the support aggregate.
        """
        params = self.validated_params()
        return Response(support_summary(params.get("start"), params.get("end")))


class ReviewsReportView(AnalyticsAPIView):
    """Return review and rating figures for the period."""

    def get(self, request):
        """Return the reviews report.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the reviews aggregate.
        """
        params = self.validated_params()
        return Response(reviews_summary(params.get("start"), params.get("end")))


class TrafficReportView(AnalyticsAPIView):
    """Return storefront-traffic figures for the period."""

    def get(self, request):
        """Return the traffic report.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the traffic aggregate.
        """
        params = self.validated_params()
        return Response(traffic_summary(params.get("start"), params.get("end")))


class NotificationsReportView(AnalyticsAPIView):
    """Return notification-send figures for the period."""

    def get(self, request):
        """Return the notifications report.

        Args:
            request: the GET request with optional ``from``/``to``.

        Returns:
            Response: the notifications aggregate.
        """
        params = self.validated_params()
        return Response(notifications_summary(params.get("start"), params.get("end")))
