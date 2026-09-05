"""Serializers for the social proof app.

Inputs are explicitly whitelisted: the viewer and recent-sales feeds accept
exactly the fields declared here, and the recording endpoint now takes no
payload fields at all (a viewer's identity comes from the server-issued
cookie, never from the client). Outputs declare a fixed shape so the frontend
can rely on stable field names.
"""

from rest_framework import serializers

from apps.social_proof.constants import BATCH_VIEWERS_MAX_SLUGS, RECENT_SALES_MAX_ITEMS


class LiveViewerCountSerializer(serializers.Serializer):
    """Response shape for a single product's live-viewer count."""

    live_viewers = serializers.IntegerField(min_value=0)


class BatchViewersQuerySerializer(serializers.Serializer):
    """Validate the ``?products=slug1,slug2`` query for the batch endpoint.

    Parses the comma-separated list into distinct slugs and bounds its size so
    one request cannot fan out to an unbounded number of cache reads.
    """

    products = serializers.CharField(max_length=2000)

    def validate_products(self, value):
        """Split the comma-separated slugs and enforce the size cap.

        Args:
            value (str): the raw ``products`` query value.

        Returns:
            list: distinct, non-empty slugs.

        Raises:
            serializers.ValidationError: when no slug remains, or more than
                the configured maximum were supplied.
        """
        slugs = [part.strip() for part in value.split(",") if part.strip()]
        if not slugs:
            raise serializers.ValidationError("At least one product slug is required.")
        if len(slugs) > BATCH_VIEWERS_MAX_SLUGS:
            raise serializers.ValidationError(
                f"No more than {BATCH_VIEWERS_MAX_SLUGS} products per request."
            )
        return list(dict.fromkeys(slugs))


class RecentSalesQuerySerializer(serializers.Serializer):
    """Validate the optional ``limit`` for the recent-sales feed."""

    limit = serializers.IntegerField(
        min_value=1, max_value=RECENT_SALES_MAX_ITEMS, default=RECENT_SALES_MAX_ITEMS
    )


class RecentSaleSerializer(serializers.Serializer):
    """One completed purchase line in the recent-sales feed.

    Deliberately excludes every customer field: only the product, quantity,
    and purchase time are surfaced.
    """

    product_slug = serializers.CharField(source="product.slug", allow_null=True)
    product_name = serializers.CharField()
    quantity = serializers.IntegerField()
    placed_at = serializers.DateTimeField(source="order.placed_at")
