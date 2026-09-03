

import django.core.exceptions as django_exc
from rest_framework import generics, permissions
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.bundles.models import Bundle, BundleItem
from apps.bundles.selectors import _with_items, get_bundle_by_slug, list_bundles
from apps.bundles.serializers import (
    BundleItemSerializer,
    BundleListSerializer,
    BundlePriceSerializer,
    BundleSerializer,
)
from apps.bundles.services import get_bundle_price

# Public views


class BundleListView(generics.ListAPIView):
    """List the bundles offered to the storefront (public).

    Only active, in-window bundles are returned. The list omits the nested
    item payload and is paginated so it stays small on a slow connection.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = BundleListSerializer

    def get_queryset(self):
        """Return active bundles offered to the storefront."""
        return list_bundles(active_only=True)


class BundleDetailView(generics.RetrieveAPIView):
    """Retrieve one bundle with its components (public).

    Returns 404 for missing, inactive, or out-of-window bundles.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = BundleSerializer

    def get_object(self):
        """Resolve the bundle by slug with active-window handling.

        Returns:
            Bundle: the active bundle.

        Raises:
            NotFound: if the slug is missing or the bundle is not live.
        """
        bundle = get_bundle_by_slug(self.kwargs["slug"], active_only=True)
        if bundle is None:
            raise NotFound("No such bundle.")
        return bundle


class BundlePriceView(APIView):
    """Return the authoritative price for a bundle (public).

    The storefront calls this to display a bundle's price. The amount is
    computed server-side from current variant prices and the bundle's own
    discount — never from a client-supplied value.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get(self, request, slug):
        """Return the computed price breakdown for the bundle.

        A bundle that is active but cannot be priced (e.g. it has no items or
        its components were removed) is treated as not offerable rather than
        surfacing an internal validation error on the storefront.

        Args:
            request: the GET request.
            slug (str): the bundle slug.

        Returns:
            Response: the price breakdown, or 404.
        """
        bundle = get_bundle_by_slug(slug, active_only=True)
        if bundle is None:
            raise NotFound("No such bundle.")
        try:
            price_data = get_bundle_price(bundle)
        except django_exc.ValidationError as exc:
            raise NotFound("Bundle is not currently offerable.") from exc
        serializer = BundlePriceSerializer(bundle)
        return Response({**serializer.data, **price_data})


# Admin views


class AdminBundleListCreateView(generics.ListCreateAPIView):
    """List all bundles or create a new one (admin only).

    The list prefetches each bundle's items so the nested payload does not
    trigger one query per bundle.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = BundleSerializer

    def get_queryset(self):
        """Return all bundles with their items pre-fetched."""
        return _with_items(Bundle.objects.all())


class AdminBundleDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a bundle (admin only).

    The single-object queryset prefetches items so the nested serialized
    payload does not cause extra queries.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = BundleSerializer

    def get_queryset(self):
        """Return a bundle queryset with items pre-fetched."""
        return _with_items(Bundle.objects.all())


class AdminBundleItemListCreateView(generics.ListCreateAPIView):
    """List or create component items for a bundle (admin only).

    ``bundle`` is set from the URL, not the request body, so an item can only
    be created against the bundle in the path.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = BundleItemSerializer

    def get_parent_bundle(self):
        """Return the parent bundle from the URL, or 404."""
        return generics.get_object_or_404(Bundle, pk=self.kwargs["bundle_pk"])

    def get_queryset(self):
        """Return items belonging to the parent bundle."""
        return BundleItem.objects.filter(bundle_id=self.kwargs["bundle_pk"])

    def perform_create(self, serializer):
        """Attach the item to the parent bundle from the URL."""
        bundle = self.get_parent_bundle()
        serializer.save(bundle=bundle)


class AdminBundleItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a bundle item (admin only).

    Scoped to the parent bundle in the URL so an item can only be addressed
    through its own bundle.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = BundleItemSerializer

    def get_queryset(self):
        """Return items belonging to the parent bundle."""
        return BundleItem.objects.filter(bundle_id=self.kwargs["bundle_pk"])
