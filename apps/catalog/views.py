"""API views for the catalog app.

Split into public browse views (``AllowAny``) and admin CRUD views
(``IsAdminUser``). Every view delegates to a service or selector function
and stays thin: parse input, call service, return response.

Public product search uses the catalog search module: synonym expansion,
index-backed trigram matching on PostgreSQL, and relevance tiers (exact name,
then prefix, then best similarity across name, brand, category, and SKU). The
suggestions endpoint returns a small, cached payload for the storefront
autocomplete.
"""

import hashlib

from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog import cache as catalog_cache
from apps.catalog.filters import AdminProductFilter, ProductFilter
from apps.catalog.models import (
    Brand,
    Category,
    FacetDefinition,
    PricingTier,
    Product,
    ProductImage,
    RelatedProduct,
)
from apps.catalog.search import (
    expand_search_terms,
    filter_products_by_search,
    normalize_search_term,
    order_products_by_search,
)
from apps.catalog.selectors import (
    get_active_brands,
    get_active_categories,
    get_all_products_admin,
    get_product_by_slug,
    get_product_list_queryset,
    order_by_popularity,
)
from apps.catalog.serializers import (
    BrandDetailSerializer,
    BrandListSerializer,
    BrandWriteSerializer,
    CategoryDetailSerializer,
    CategoryListSerializer,
    CategorySuggestionSerializer,
    CategoryWriteSerializer,
    FacetDefinitionSerializer,
    PricingTierSerializer,
    PricingTierWriteSerializer,
    ProductDetailSerializer,
    ProductFAQListSerializer,
    ProductFAQReplaceSerializer,
    ProductFAQStaffSerializer,
    ProductImageSerializer,
    ProductImageWriteSerializer,
    ProductListSerializer,
    ProductSuggestionSerializer,
    ProductWriteSerializer,
    RelatedProductSerializer,
    RelatedProductWriteSerializer,
)
from apps.catalog.services import (
    compute_facet_counts,
    validate_facet_params,
)
from apps.core.permissions import IsStaffUser
from apps.core.throttling import ClientIPScopedRateThrottle as ScopedRateThrottle

# Public browse views


def _hash_query_params(query_params):
    """Return a stable hash of the request query params for page caching.

    Sorts keys and multi-values so semantically identical filter
    combinations share one cache entry regardless of param order.

    Args:
        query_params (QueryDict): the request query parameters.

    Returns:
        str: the hex digest identifying this filter combination.
    """
    normalized = sorted((key, sorted(values)) for key, values in query_params.lists())
    return hashlib.sha1(repr(normalized).encode()).hexdigest()


class CatalogPagination(PageNumberPagination):
    """Bounded page-size control for storefront catalog reads.

    Allows small rows for home/category shelves (``page_size=8``) without
    letting a caller request an unbounded list.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 40


class CategoryListView(APIView):
    """List active categories for public browsing.

    Returns a flat list of active categories with product counts. The list
    is not paginated  -  category trees are bounded. The serialized rows are
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
                        queryset=Category.objects.filter(is_active=True)
                        .annotate(product_count=Count("products"))
                        .order_by("sort_order", "name"),
                    )
                ),
                slug=slug,
            )
            data = CategoryDetailSerializer(category).data
            catalog_cache.cache_category_detail(slug, generation, data)
        return Response(data)


class BrandListView(generics.ListAPIView):
    """List active brands for public browsing.

    Not paginated  -  the brand list is bounded.
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

    Search is typo-tolerant and synonym-aware on PostgreSQL, backed by the
    trigram indexes on name, SKU, and descriptions. ``ordering=popular``
    orders by real view counts from the last 30 days; an explicit ordering
    always wins over relevance.

    Faceted filtering validates query params against active
    ``FacetDefinition`` rows before applying JSON field lookups, preventing
    arbitrary or unregistered spec keys from being used as filters. Base
    filters also cover brand, price range, and in-stock only.

    The response includes a ``facets`` dict with aggregate counts per active
    facet, computed from the currently-filtered queryset.

    The serialized page is cached per filter combination under the
    product-list generation (see ``apps.catalog.cache``); any product,
    category, image, or facet change invalidates every cached page.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"
    serializer_class = ProductListSerializer
    pagination_class = CatalogPagination
    filterset_class = ProductFilter
    ordering_fields = [
        "name",
        "created_at",
        "average_rating",
        "review_count",
        "price",
    ]

    def get_queryset(self):
        """Return the optimised active-product queryset."""
        return get_product_list_queryset()

    def list(self, request, *args, **kwargs):
        """Override to include faceted counts in the response.

        Applies validated facet filters to the base queryset, applies the
        typo-tolerant search filter, orders text searches by relevance unless
        the caller asked for an explicit ordering, then computes aggregate
        counts for all active facets.

        The whole serialized page (rows, facets, pagination envelope) is
        identical for every anonymous caller asking the same filters, so it
        is served from the product-list page cache when current. A warm
        page costs two cache reads instead of the paginator count, the
        page select, and the facet aggregates.
        """
        generation = catalog_cache.get_product_list_generation()
        query_hash = _hash_query_params(request.query_params)
        cached = catalog_cache.get_cached_product_list_page(generation, query_hash)
        if cached is not None:
            return Response(cached)

        queryset = self.filter_queryset(self.get_queryset())
        queryset = queryset.filter(validate_facet_params(request.query_params))
        search_term = (request.query_params.get("search") or "").strip()
        ordering = (request.query_params.get("ordering") or "").strip()
        if search_term:
            queryset = filter_products_by_search(queryset, search_term)
        if ordering == "popular":
            queryset = order_by_popularity(queryset)
        elif search_term and not ordering:
            queryset = order_products_by_search(queryset, search_term)

        page = self.paginate_queryset(queryset)
        if page is not None:
            from apps.promotions.services import get_effective_prices

            serializer = self.get_serializer(
                page,
                many=True,
                context={
                    **self.get_serializer_context(),
                    "effective_prices": get_effective_prices(page),
                },
            )
            facet_counts = compute_facet_counts(queryset)
            response = self.get_paginated_response(serializer.data)
            response.data["facets"] = facet_counts
            catalog_cache.cache_product_list_page(generation, query_hash, response.data)
            return response

        capped = queryset[: getattr(self.paginator, "page_size", 20)]
        from apps.promotions.services import get_effective_prices

        products = list(capped)
        serializer = self.get_serializer(
            products,
            many=True,
            context={
                **self.get_serializer_context(),
                "effective_prices": get_effective_prices(products),
            },
        )
        facet_counts = compute_facet_counts(queryset)
        payload = {"results": serializer.data, "facets": facet_counts}
        catalog_cache.cache_product_list_page(generation, query_hash, payload)
        return Response(payload)


class ProductSuggestionView(APIView):
    """Return a small autocomplete payload for the storefront search box.

    The query must be at least two normalized characters; shorter input gets
    an empty payload instead of a database query. Results are the top six
    matching products (thumbnail and server price only) plus up to four
    matching categories, cached briefly per normalized query. The endpoint has
    its own throttle scope well above the catalog read budget because the
    storefront calls it as the visitor types.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "catalog_suggest"

    @extend_schema(
        operation_id="product_suggestions",
        responses={200: dict},
    )
    def get(self, request):
        """Return products and categories matching the query.

        Args:
            request: the GET request with the ``q`` query parameter.

        Returns:
            Response: ``200 OK`` with ``query``, ``products``, and
                ``categories``. The product list is empty below the minimum
                query length.
        """
        raw_query = (request.query_params.get("q") or "").strip()
        normalized = normalize_search_term(raw_query)
        if len(normalized) < 2:
            return Response({"query": raw_query, "products": [], "categories": []})

        cached = catalog_cache.get_cached_suggestions(normalized)
        if cached is not None:
            return Response(cached)

        products = list(
            order_products_by_search(
                filter_products_by_search(get_product_list_queryset(), normalized),
                normalized,
            )[:6]
        )

        category_query = Q()
        for term in expand_search_terms(normalized):
            category_query |= Q(name__icontains=term)
        categories = Category.objects.filter(category_query, is_active=True).order_by(
            "sort_order", "name"
        )[:4]
        from apps.promotions.services import get_effective_prices

        payload = {
            "query": raw_query,
            "products": ProductSuggestionSerializer(
                products,
                many=True,
                context={"effective_prices": get_effective_prices(products)},
            ).data,
            "categories": CategorySuggestionSerializer(categories, many=True).data,
        }
        catalog_cache.cache_suggestions(normalized, payload)
        return Response(payload)


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
        product = get_product_by_slug(
            self.kwargs["slug"], include_inactive=False, include_faqs=True
        )
        if product is None:
            from django.http import Http404

            raise Http404
        return product


class ProductPriceView(APIView):
    """Return the effective price of a product.

    Public endpoint showing the base price, compare-at price, and volume
    pricing tiers. The storefront can call this once per listing card, so it
    has its own per-IP budget rather than sharing the catalog read budget.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "product_price"

    @extend_schema(
        operation_id="product_price",
        responses={200: dict},
    )
    def get(self, request, slug):
        """Return the effective pricing data of the product.

        The advertised price is the server-computed effective price (catalogue
        price after the best applicable discount), never the raw stored price
         -  a storefront that wires this endpoint into a checkout must never
        show or charge a discount-ignoring amount.

        Args:
            request: the GET request.
            slug (str): the product slug.

        Returns:
            Response: 200 with the product pricing, or 404.
        """
        from apps.promotions.services import get_effective_price

        product = get_product_by_slug(slug, include_inactive=False, include_faqs=False)
        if product is None:
            from django.http import Http404

            raise Http404

        tiers = product.pricing_tiers.all()
        pricing = get_effective_price(product)

        return Response(
            {
                "product_slug": slug,
                "product": {
                    "id": product.id,
                    "sku": product.sku,
                    "price": pricing["price"],
                    "base_price": pricing["base_price"],
                    "discount": pricing["discount"],
                    "compare_at_price": (
                        str(product.compare_at_price)
                        if product.compare_at_price
                        else None
                    ),
                    "pricing_tiers": [
                        {
                            "min_quantity": t.min_quantity,
                            "unit_price": str(t.unit_price),
                        }
                        for t in tiers
                    ],
                },
            }
        )


# Admin CRUD views


class AdminCategoryListCreateView(generics.ListCreateAPIView):
    """List all categories or create a new one (admin only)."""

    permission_classes = [IsStaffUser]
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
            .order_by("sort_order", "name")
        )


class AdminCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a category (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    serializer_class = CategoryWriteSerializer
    queryset = Category.objects.all()

    def perform_destroy(self, instance):
        """Delete the category."""
        instance.delete()


class AdminBrandListCreateView(generics.ListCreateAPIView):
    """List all brands or create a new one (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return BrandListSerializer
        return BrandWriteSerializer

    def get_queryset(self):
        return get_active_brands()


class AdminBrandDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a brand (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    serializer_class = BrandWriteSerializer
    queryset = Brand.objects.all()


class AdminProductListCreateView(generics.ListCreateAPIView):
    """List all products or create a new one (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    filterset_class = AdminProductFilter
    search_fields = ["name", "sku", "slug"]

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ProductListSerializer
        return ProductWriteSerializer

    def get_queryset(self):
        return get_all_products_admin()


class AdminProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a product (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    serializer_class = ProductWriteSerializer

    def get_queryset(self):
        return get_all_products_admin()


class AdminProductFAQView(APIView):
    """Read or fully replace the ordered FAQ list for one product (staff only).

    The parent product is taken from the URL, so a caller cannot write FAQs
    to a product they did not address. Replacement is atomic: validation
    happens before any row is deleted.
    """

    permission_classes = [IsStaffUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"

    def get_product(self):
        """Return the parent product from the URL, or 404.

        Returns:
            Product: the addressed product.
        """
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    @extend_schema(
        operation_id="admin_product_faq_list",
        responses={200: ProductFAQListSerializer},
    )
    def get(self, request, product_pk):
        """Return the product's FAQs in display order.

        Args:
            request: the GET request.
            product_pk (int): the parent product primary key.

        Returns:
            Response: ``200 OK`` with ``{"faqs": [...]}``.
        """
        product = self.get_product()
        faqs = product.faqs.all()
        return Response({"faqs": ProductFAQStaffSerializer(faqs, many=True).data})

    @extend_schema(
        operation_id="admin_product_faq_replace",
        request=ProductFAQReplaceSerializer,
        responses={200: ProductFAQListSerializer},
    )
    def put(self, request, product_pk):
        """Replace all FAQs for the product.

        Args:
            request: the PUT request with a ``faqs`` list.
            product_pk (int): the parent product primary key.

        Returns:
            Response: ``200 OK`` with the saved FAQ list.
        """
        product = self.get_product()
        serializer = ProductFAQReplaceSerializer(
            data=request.data, context={"product": product}
        )
        serializer.is_valid(raise_exception=True)

        from apps.catalog.services import replace_product_faqs

        faqs = replace_product_faqs(product, serializer.validated_data["faqs"])
        return Response({"faqs": ProductFAQStaffSerializer(faqs, many=True).data})


class AdminProductImageListCreateView(generics.ListCreateAPIView):
    """List or create images for a specific product (admin only).

    ``product`` is set from the URL, not the request body.
    """

    permission_classes = [IsStaffUser]
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

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    queryset = ProductImage.objects.all()


class AdminProductImageDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete an image of a product (admin only).

    Scoped to the parent product in the URL so an image can only be
    addressed through its own product.
    """

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return ProductImageSerializer
        return ProductImageWriteSerializer

    def get_queryset(self):
        """Return images belonging to the parent product."""
        return ProductImage.objects.filter(product_id=self.kwargs["product_pk"])


class AdminPricingTierListCreateView(generics.ListCreateAPIView):
    """List or create pricing tiers for a specific product (admin only).

    The ``product`` association comes from the URL, not the request body.
    """

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    serializer_class = PricingTierWriteSerializer

    def get_parent_product(self):
        """Return the parent product from the URL, or 404."""
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    def get_queryset(self):
        """Return tiers belonging to the parent product."""
        return PricingTier.objects.filter(product_id=self.kwargs["product_pk"])

    def perform_create(self, serializer):
        """Attach the tier to the parent product from the URL."""
        product = self.get_parent_product()
        serializer.save(product=product)


@extend_schema(responses={204: None})
class AdminPricingTierDeleteView(generics.DestroyAPIView):
    """Delete a pricing tier (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    queryset = PricingTier.objects.all()


class AdminPricingTierDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a pricing tier (admin only).

    Scoped so a tier can only be addressed through the product that owns it.
    """

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"

    def get_serializer_class(self):
        if self.request.method == "GET":
            return PricingTierSerializer
        return PricingTierWriteSerializer

    def get_queryset(self):
        """Return tiers belonging to the parent product."""
        return PricingTier.objects.filter(product_id=self.kwargs["product_pk"])


class AdminRelatedProductListCreateView(generics.ListCreateAPIView):
    """List or create related products for a specific product (admin only).

    ``product`` is set from the URL; ``related_product`` is validated to
    differ from the parent.
    """

    permission_classes = [IsStaffUser]
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

    permission_classes = [IsStaffUser]
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

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    serializer_class = FacetDefinitionSerializer
    queryset = FacetDefinition.objects.all().order_by("sort_order", "name")


class AdminFacetDefinitionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a facet definition (admin only)."""

    permission_classes = [IsStaffUser]
    throttle_scope = "admin"
    serializer_class = FacetDefinitionSerializer
    queryset = FacetDefinition.objects.all()
