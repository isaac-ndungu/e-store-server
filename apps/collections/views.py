"""API views for the collections app.

Split into public endpoints (``AllowAny``: collection list and a single
collection's detail with its products) and admin CRUD views (``IsAdminUser``)
for collections and their membership rows. Views stay thin: parse input, call
a service or selector, return a response. The public detail endpoint reads
membership from the Redis cache when warm so the storefront stays fast.
"""

from rest_framework import generics, permissions
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.catalog.serializers import ProductListSerializer
from apps.collections.models import Collection, CollectionMembership
from apps.collections.selectors import (
    get_collection_by_slug,
    get_collection_products,
    list_collections,
)
from apps.collections.serializers import (
    CollectionMembershipSerializer,
    CollectionSerializer,
)

# Public views


class CollectionListView(generics.ListAPIView):
    """List the collections offered to the storefront (public).

    Only active collections are returned, optionally narrowed by
    ``collection_type`` or ``display_location``.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = CollectionSerializer

    def get_queryset(self):
        """Return active collections, optionally filtered."""
        return list_collections(
            active_only=True,
            collection_type=self.request.query_params.get("collection_type"),
            display_location=self.request.query_params.get("display_location"),
        )


class CollectionDetailView(generics.RetrieveAPIView):
    """Retrieve one collection with its products (public).

    The product list reads from the per-slug Redis cache when warm and falls
    back to the stored membership rows. Products are serialized with the slim
    list serializer so the payload stays small for data-conscious shoppers.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = CollectionSerializer

    def get_object(self):
        """Resolve the collection by slug with active-window handling.

        Returns:
            Collection: the active collection.

        Raises:
            NotFound: if the slug is missing or the collection is inactive.
        """
        collection = get_collection_by_slug(self.kwargs["slug"], active_only=True)
        if collection is None:
            raise NotFound("No such collection.")
        return collection

    def retrieve(self, request, *args, **kwargs):
        """Return the collection payload plus its products.

        Args:
            request: the incoming request.

        Returns:
            Response: the collection serialized with a ``products`` array.
        """
        collection = self.get_object()
        serializer = self.get_serializer(collection)
        products = get_collection_products(collection)
        product_serializer = ProductListSerializer(products, many=True)
        return Response({**serializer.data, "products": product_serializer.data})


# Admin views


class AdminCollectionListCreateView(generics.ListCreateAPIView):
    """List all collections or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = CollectionSerializer

    def get_queryset(self):
        """Return all collections (active and inactive) for management."""
        return list_collections(
            collection_type=self.request.query_params.get("collection_type")
        )


class AdminCollectionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a collection (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = CollectionSerializer
    queryset = Collection.objects.all()


class AdminCollectionMembershipListCreateView(generics.ListCreateAPIView):
    """List or create membership rows for a collection (admin only).

    Supports an optional ``collection`` query filter. Creating a row on a
    smart collection is allowed so staff can hand-tune an auto collection.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = CollectionMembershipSerializer

    def get_queryset(self):
        """Return memberships, optionally scoped to one collection."""
        queryset = CollectionMembership.objects.select_related("product")
        collection = self.request.query_params.get("collection")
        if collection is not None:
            queryset = queryset.filter(collection_id=collection)
        return queryset


class AdminCollectionMembershipDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a collection membership row (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = CollectionMembershipSerializer

    def get_queryset(self):
        """Return membership rows pre-fetched for their product."""
        return CollectionMembership.objects.select_related("product")
