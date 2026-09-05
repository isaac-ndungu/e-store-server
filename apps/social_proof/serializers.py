"""Serializers for the social proof app.

The recording endpoint accepts an optional client-supplied session identifier
and returns the refreshed live-viewer count; the public viewer-count endpoint
returns the same shape. No serializer exposes raw event rows to the
storefront.
"""

from rest_framework import serializers


class ProductViewSerializer(serializers.Serializer):
    """Validate input to the product-view recording endpoint."""

    session_key = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
