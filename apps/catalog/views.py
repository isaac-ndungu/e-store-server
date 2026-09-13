"""API views for the catalog app.

Split into public browse views (``AllowAny``) and admin CRUD views
(``IsAdminUser``). Every view delegates to a service or selector function
and stays thin: parse input, call service, return response.

Public product search uses DRF's ``SearchFilter`` (``icontains`` matching)
as an interim implementation. Full-text search with autocomplete and typo
tolerance (Meilisearch/Typesense) is planned for a later step.
"""

from django.db.models import Count, Prefetch
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.catalog import cache as catalog_cache
from apps.catalog.models import (
    Brand,
    Category,
    FacetDefinition,
    PricingTier,
    Product,
    ProductImage,
    ProductVariant,
    RelatedProduct,
)
from apps.catalog.selectors import (
    get_active_brands,
    get_active_categories,
    get_all_products_admin,
    get_product_by_slug,
    get_product_list_queryset,
    order_by_search_rank,
)
from apps.catalog.serializers import (
    BrandDetailSerializer,
    BrandListSerializer,
    BrandWriteSerializer,
    CategoryDetailSerializer,
    CategoryListSerializer,
    CategoryWriteSerializer,
    FacetDefinitionSerializer,
    PricingTierSerializer,
    PricingTierWriteSerializer,
    ProductDetailSerializer,
    ProductImageSerializer,
    ProductImageWriteSerializer,
    ProductListSerializer,
    ProductVariantDetailSerializer,
    ProductVariantWriteSerializer,
    ProductWriteSerializer,
    RelatedProductSerializer,
    RelatedProductWriteSerializer,
)
from apps.catalog.services import (
    compute_facet_counts,
    validate_facet_params,
)

# Public browse views


class CategoryListView(APIView):
    """List active categories for public browsing.

    Returns a flat list of active categories with product counts. The list
    is not paginated — category trees are bounded. The serialized rows are
    served from the category cache when current, avoiding the count query
    on every storefront load.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="category_list",
        responses={200: CategoryListSerializer(many=True)},
    )
    def get(self, request):
        """Return active categories, optionally filtered by parent.

        Args:
            request: the GET request with an optional ``parent`` query.

        Returns:
            Response: the serialized category rows.

        Raises:
            DRFValidationError: if the ``parent`` param is not a valid id
                or the ``null`` marker.
        """
        generation = catalog_cache.get_category_generation()
        rows = catalog_cache.get_cached_category_rows(generation)
        if rows is None:
            rows = CategoryListSerializer(get_active_categories(), many=True).data
            catalog_cache.cache_category_rows(generation, rows)

        parent = request.query_params.get("parent")
        if parent is not None:
            if parent in ("", "null"):
                rows = [row for row in rows if row["parent"] is None]
            else:
                if not parent.isdigit():
                    raise DRFValidationError(
                        {"parent": "parent must be a category id or 'null'."}
                    )
                rows = [row for row in rows if row["parent"] == int(parent)]
        return Response(rows)


class CategoryDetailView(generics.RetrieveAPIView):
    """Retrieve a single active category by slug.

    Returns 404 for inactive or missing categories.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="category_detail",
        responses={200: CategoryDetailSerializer},
    )
    def get(self, request, slug):
        """Return the active category with its children and product counts.

        The serialized payload is served from the category cache when
        current; a cold cache builds the annotated tree and repopulates it
        under the current generation.

        Args:
            request: the GET request.
            slug (str): the category slug.

        Returns:
            Response: the category detail, or 404 when missing or inactive.
        """
        generation = catalog_cache.get_category_generation()
        data = catalog_cache.get_cached_category_detail(slug, generation)
        if data is None:
            category = get_object_or_404(
                Category.objects.filter(is_active=True)
                .select_related("parent")
                .annotate(product_count=Count("products"))
                .prefetch_related(
                    Prefetch(
                        "children",
                        queryset=Category.objects.filter(is_active=True).annotate(
                            product_count=Count("products")
                        ),
                    )
                ),
                slug=slug,
            )
            data = CategoryDetailSerializer(category).data
            catalog_cache.cache_category_detail(slug, generation, data)
        return Response(data)


class BrandListView(generics.ListAPIView):
    """List active brands for public browsing.

    Not paginated — the brand list is bounded.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"
    serializer_class = BrandListSerializer
    pagination_class = None
    queryset = get_active_brands()


class BrandDetailView(generics.RetrieveAPIView):
    """Retrieve a single brand by slug."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"
    serializer_class = BrandDetailSerializer
    lookup_field = "slug"
    queryset = get_active_brands()


class ProductListView(generics.ListAPIView):
    """List active products with search, filtering, ordering, and faceted counts.

    Uses DRF's ``SearchFilter`` (``icontains`` on name, description,
    short_description, sku) for matching, with Postgres full-text rank
    ordering applied on top when the database supports it. A dedicated
    external engine with typo tolerance and autocomplete stays deferred
    until catalog size and traffic justify the extra service.

    Faceted filtering validates query params against active
    ``FacetDefinition`` rows before applying JSON field lookups, preventing
    arbitrary or unregistered spec keys from being used as filters.

    The response includes a ``facets`` dict with aggregate counts per active
    facet, computed from the currently-filtered queryset.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"
    serializer_class = ProductListSerializer
    search_fields = ["name", "description", "short_description", "sku"]
    filterset_fields = ["category", "brand", "is_featured", "product_type", "tax_class"]
    ordering_fields = ["name", "created_at", "average_rating", "review_count"]

    def get_queryset(self):
        """Return the optimised active-product queryset."""
        return get_product_list_queryset()

    def list(self, request, *args, **kwargs):
        """Override to include faceted counts in the response.

        Applies validated facet filters to the base queryset, orders text
        searches by relevance unless the caller asked for an explicit
        ordering, then computes aggregate counts for all active facets.
        """
        queryset = self.filter_queryset(self.get_queryset())
        queryset = queryset.filter(validate_facet_params(request.query_params))
        if request.query_params.get("search") and not request.query_params.get(
            "ordering"
        ):
            queryset = order_by_search_rank(
                queryset, request.query_params.get("search")
            )

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            facet_counts = compute_facet_counts(queryset)
            response = self.get_paginated_response(serializer.data)
            response.data["facets"] = facet_counts
            return response

        serializer = self.get_serializer(queryset, many=True)
        facet_counts = compute_facet_counts(queryset)
        return Response({"results": serializer.data, "facets": facet_counts})


class ProductDetailView(generics.RetrieveAPIView):
    """Retrieve a single product by slug with full detail.

    Returns 404 for inactive or discontinued products. Admin detail is
    available through a separate admin endpoint.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"
    serializer_class = ProductDetailSerializer
    lookup_field = "slug"

    def get_object(self):
        """Return the active product by slug, or 404."""
        product = get_product_by_slug(self.kwargs["slug"], include_inactive=False)
        if product is None:
            from django.http import Http404

            raise Http404
        return product


class ProductPriceView(APIView):
    """Return variant pricing for a product.

    Public endpoint showing the base price, compare-at price, and pricing
    tiers for each active variant. Used by the storefront's pricing display.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    @extend_schema(
        operation_id="product_price",
        responses={200: dict},
    )
    def get(self, request, slug):
        """Return pricing data for all active variants of the product.

        The advertised price is the server-computed effective price (catalogue
        price after the best applicable discount), never the raw stored price
        — a storefront that wires this endpoint into a checkout must never
        show or charge a discount-ignoring amount.

        Args:
            request: the GET request.
            slug (str): the product slug.

        Returns:
            Response: 200 with variant pricing, or 404.
        """
        from apps.promotions.services import get_effective_price

        product = get_product_by_slug(slug, include_inactive=False)
        if product is None:
            from django.http import Http404

            raise Http404

        variants = product.variants.all()

        data = []
        for variant in variants:
            tiers = variant.pricing_tiers.all()
            pricing = get_effective_price(variant)
            data.append(
                {
                    "id": variant.id,
                    "sku": variant.sku,
                    "attributes": variant.attributes,
                    "price": pricing["price"],
                    "base_price": pricing["base_price"],
                    "discount": pricing["discount"],
                    "compare_at_price": (
                        str(variant.compare_at_price)
                        if variant.compare_at_price
                        else None
                    ),
                    "pricing_tiers": [
                        {
                            "min_quantity": t.min_quantity,
                            "unit_price": str(t.unit_price),
                        }
                        for t in tiers
                    ],
                }
            )

        return Response({"product_slug": slug, "variants": data})


# Admin CRUD views


class AdminCategoryListCreateView(generics.ListCreateAPIView):
    """List all categories or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = CategoryWriteSerializer

    def get_serializer_class(self):
        if self.request.method == "GET":
            return CategoryListSerializer
        return CategoryWriteSerializer

    def get_queryset(self):
        return (
            Category.objects.select_related("parent")
            .annotate(product_count=Count("products"))
            .order_by("name")
        )


class AdminCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a category (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = CategoryWriteSerializer
    queryset = Category.objects.all()

    def perform_destroy(self, instance):
        """Delete the category."""
        instance.delete()


class AdminBrandListCreateView(generics.ListCreateAPIView):
    """List all brands or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return BrandListSerializer
        return BrandWriteSerializer

    def get_queryset(self):
        return get_active_brands()


class AdminBrandDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a brand (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = BrandWriteSerializer
    queryset = Brand.objects.all()


class AdminProductListCreateView(generics.ListCreateAPIView):
    """List all products or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ProductListSerializer
        return ProductWriteSerializer

    def get_queryset(self):
        return get_all_products_admin()


class AdminProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a product (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = ProductWriteSerializer

    def get_queryset(self):
        return get_all_products_admin()


class AdminProductVariantListCreateView(generics.ListCreateAPIView):
    """List or create variants for a specific product (admin only).

    ``product`` is set from the URL, not the request body.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ProductVariantDetailSerializer
        return ProductVariantWriteSerializer

    def get_parent_product(self):
        """Return the parent product from the URL, or 404."""
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    def get_queryset(self):
        """Return variants belonging to the parent product."""
        return ProductVariant.objects.filter(
            product_id=self.kwargs["product_pk"]
        ).prefetch_related("pricing_tiers")

    def perform_create(self, serializer):
        """Attach the variant to the parent product from the URL."""
        product = self.get_parent_product()
        serializer.save(product=product)


class AdminProductVariantDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a variant (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = ProductVariantWriteSerializer

    def get_queryset(self):
        """Return only variants belonging to the parent product."""
        return ProductVariant.objects.filter(
            product_id=self.kwargs["product_pk"]
        ).prefetch_related("pricing_tiers")


class AdminProductImageListCreateView(generics.ListCreateAPIView):
    """List or create images for a specific product (admin only).

    ``product`` is set from the URL, not the request body.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ProductImageSerializer
        return ProductImageWriteSerializer

    def get_parent_product(self):
        """Return the parent product from the URL, or 404."""
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    def get_queryset(self):
        """Return images belonging to the parent product."""
        return ProductImage.objects.filter(product_id=self.kwargs["product_pk"])

    def perform_create(self, serializer):
        """Attach the image to the parent product from the URL."""
        product = self.get_parent_product()
        serializer.save(product=product)


@extend_schema(responses={204: None})
class AdminProductImageDeleteView(generics.DestroyAPIView):
    """Delete an image by pk (admin only).

    The endpoint is deliberately kept flat (``/catalog/admin/images/<pk>/``)
    to match the admin UI's delete flow; it is safe because it is admin-only
    and a delete of an image only ever affects that image row, never a
    sibling product's data.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    queryset = ProductImage.objects.all()


class AdminProductImageDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete an image of a product (admin only).

    Scoped to the parent product in the URL so an image can only be
    addressed through its own product.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ProductImageSerializer
        return ProductImageWriteSerializer

    def get_queryset(self):
        """Return images belonging to the parent product."""
        return ProductImage.objects.filter(product_id=self.kwargs["product_pk"])


class AdminPricingTierListCreateView(generics.ListCreateAPIView):
    """List or create pricing tiers for variants of a specific product (admin only).

    The ``variant`` FK is validated to belong to the parent product.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = PricingTierWriteSerializer

    def get_parent_product(self):
        """Return the parent product from the URL, or 404."""
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    def get_queryset(self):
        """Return tiers for variants belonging to the parent product."""
        return PricingTier.objects.filter(variant__product_id=self.kwargs["product_pk"])

    def perform_create(self, serializer):
        """Validate that the variant belongs to the parent product."""
        product = self.get_parent_product()
        variant = serializer.validated_data["variant"]
        if variant.product_id != product.pk:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {"variant": "This variant does not belong to the specified product."}
            )
        serializer.save()


@extend_schema(responses={204: None})
class AdminPricingTierDeleteView(generics.DestroyAPIView):
    """Delete a pricing tier (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    queryset = PricingTier.objects.all()


class AdminPricingTierDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a pricing tier (admin only).

    Scoped so a tier can only be addressed through the product that owns
    its variant.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return PricingTierSerializer
        return PricingTierWriteSerializer

    def get_queryset(self):
        """Return tiers for variants belonging to the parent product."""
        return PricingTier.objects.filter(variant__product_id=self.kwargs["product_pk"])

    def get_serializer_context(self):
        """Inject the parent product for variant-ownership validation."""
        context = super().get_serializer_context()
        context["parent_product"] = get_object_or_404(
            Product, pk=self.kwargs["product_pk"]
        )
        return context


class AdminRelatedProductListCreateView(generics.ListCreateAPIView):
    """List or create related products for a specific product (admin only).

    ``product`` is set from the URL; ``related_product`` is validated to
    differ from the parent.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return RelatedProductSerializer
        return RelatedProductWriteSerializer

    def get_parent_product(self):
        """Return the parent product from the URL, or 404."""
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    def get_queryset(self):
        """Return related-product links from the parent product."""
        return RelatedProduct.objects.filter(product_id=self.kwargs["product_pk"])

    def get_serializer_context(self):
        """Pass the parent product for self-referential validation."""
        context = super().get_serializer_context()
        context["parent_product"] = self.get_parent_product()
        return context

    def perform_create(self, serializer):
        """Attach the related-product link to the parent product."""
        product = self.get_parent_product()
        serializer.save(product=product)


class AdminRelatedProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a related-product link (admin only).

    Scoped to the parent product in the URL.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return RelatedProductSerializer
        return RelatedProductWriteSerializer

    def get_queryset(self):
        """Return related-product links from the parent product."""
        return RelatedProduct.objects.filter(product_id=self.kwargs["product_pk"])

    def get_serializer_context(self):
        """Pass the parent product for self-referential and duplicate checks."""
        context = super().get_serializer_context()
        context["parent_product"] = get_object_or_404(
            Product, pk=self.kwargs["product_pk"]
        )
        return context


class AdminFacetDefinitionListCreateView(generics.ListCreateAPIView):
    """List all facet definitions or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = FacetDefinitionSerializer
    queryset = FacetDefinition.objects.all().order_by("sort_order", "name")


class AdminFacetDefinitionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a facet definition (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "admin"
    serializer_class = FacetDefinitionSerializer
    queryset = FacetDefinition.objects.all()
