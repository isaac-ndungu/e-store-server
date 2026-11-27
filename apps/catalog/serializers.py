from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.catalog.models import (
    Brand,
    Category,
    FacetDefinition,
    PricingTier,
    Product,
    ProductFAQ,
    ProductImage,
    RelatedProduct,
)

# Category serializers.


class CategoryListSerializer(serializers.ModelSerializer):
    """Slim category representation for list endpoints."""

    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "image",
            "is_active",
            "parent",
            "sort_order",
            "product_count",
        ]
        read_only_fields = ["id"]

    def get_product_count(self, obj):
        """Return the annotated product count, falling back to a direct count.

        Args:
            obj (Category): the category instance.

        Returns:
            int: the number of products in the category.
        """
        if hasattr(obj, "product_count"):
            return obj.product_count
        return obj.products.count()


class CategoryDetailSerializer(serializers.ModelSerializer):
    """Full category representation including sub-categories."""

    children = CategoryListSerializer(many=True, read_only=True)
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "image",
            "meta_title",
            "meta_description",
            "is_active",
            "parent",
            "sort_order",
            "children",
            "product_count",
        ]
        read_only_fields = ["id"]

    def get_product_count(self, obj):
        """Return the annotated product count, falling back to a direct count.

        Args:
            obj (Category): the category instance.

        Returns:
            int: the number of products in the category.
        """
        if hasattr(obj, "product_count"):
            return obj.product_count
        return obj.products.count()


class CategoryWriteSerializer(serializers.ModelSerializer):
    """Writable fields for creating/updating a category (admin only).

    ``slug`` is optional on create  -  if omitted it is auto-generated from
    ``name``.
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
        max_length=Category._meta.get_field("slug").max_length,
        validators=[
            UniqueValidator(
                queryset=Category.objects.all(),
                message="A category with this slug already exists.",
            )
        ],
    )

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "image",
            "meta_title",
            "meta_description",
            "is_active",
            "parent",
            "sort_order",
        ]
        read_only_fields = ["id"]

    def validate_image(self, value):
        """Validate the uploaded image via the shared validator."""
        from apps.catalog.validators import validate_image_upload

        validate_image_upload(value)
        return value

    def validate(self, attrs):
        """Reject a parent change that would cycle back to the category.

        Walks the proposed parent's ancestor chain to confirm it never
        reaches the category itself. Only runs on updates that move the
        category (a fresh category cannot create a cycle, and the model
        constraint already blocks self-parenting).

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the parent choice creates a cycle.
        """
        parent = attrs.get("parent")
        if parent is None or self.instance is None:
            return attrs

        node_id = parent.pk
        visited = set()
        while node_id is not None:
            if node_id == self.instance.pk:
                raise serializers.ValidationError(
                    {"parent": "A category cannot be its own parent or ancestor."}
                )
            if node_id in visited:
                break
            visited.add(node_id)
            node_id = (
                Category.objects.filter(pk=node_id)
                .values_list("parent_id", flat=True)
                .first()
            )
        return attrs

    def create(self, validated_data):
        """Create the category, generating a slug from the name if omitted."""
        from apps.catalog.services import create_category

        return create_category(**validated_data)

    def update(self, instance, validated_data):
        """Update the category, regenerating the slug if the name changes."""
        from apps.catalog.services import update_category

        return update_category(instance, **validated_data)


# Brand serializers.


class BrandListSerializer(serializers.ModelSerializer):
    """Slim brand representation for list endpoints."""

    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = [
            "id",
            "name",
            "slug",
            "logo",
            "is_authorized_dealer",
            "product_count",
        ]
        read_only_fields = ["id"]

    def get_product_count(self, obj):
        """Return the annotated product count, falling back to a direct count.

        Args:
            obj (Brand): the brand instance.

        Returns:
            int: the number of products in the brand.
        """
        if hasattr(obj, "product_count"):
            return obj.product_count
        return obj.products.count()


class BrandDetailSerializer(serializers.ModelSerializer):
    """Full brand representation."""

    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = [
            "id",
            "name",
            "slug",
            "logo",
            "description",
            "is_authorized_dealer",
            "product_count",
        ]
        read_only_fields = ["id"]

    def get_product_count(self, obj):
        """Return the annotated product count, falling back to a direct count.

        Args:
            obj (Brand): the brand instance.

        Returns:
            int: the number of products in the brand.
        """
        if hasattr(obj, "product_count"):
            return obj.product_count
        return obj.products.count()


class BrandWriteSerializer(serializers.ModelSerializer):
    """Writable fields for creating/updating a brand (admin only).

    ``slug`` is optional on create  -  if omitted it is auto-generated from
    ``name``.
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
        max_length=Brand._meta.get_field("slug").max_length,
        validators=[
            UniqueValidator(
                queryset=Brand.objects.all(),
                message="A brand with this slug already exists.",
            )
        ],
    )

    class Meta:
        model = Brand
        fields = [
            "id",
            "name",
            "slug",
            "logo",
            "description",
            "is_authorized_dealer",
        ]
        read_only_fields = ["id"]

    def validate_image(self, value):
        """Validate the uploaded logo via the shared validator."""
        from apps.catalog.validators import validate_image_upload

        validate_image_upload(value)
        return value

    def create(self, validated_data):
        """Create the brand, generating a slug from the name if omitted."""
        from apps.catalog.services import create_brand

        return create_brand(**validated_data)

    def update(self, instance, validated_data):
        """Update the brand, regenerating the slug if the name changes."""
        from apps.catalog.services import update_brand

        return update_brand(instance, **validated_data)


# Product serializers.


class ProductImageSerializer(serializers.ModelSerializer):
    """Read serializer for product images with processed-variant info.

    ``default_image`` is the URL the storefront should display and ``srcset``
    lists the responsive variants (width, height, format, and URL) generated
    from the upload. Browsers pick from ``srcset``; the original upload is
    not meant to be served to customers.
    """

    default_image = serializers.SerializerMethodField()
    srcset = serializers.JSONField(source="image_sources", read_only=True)

    class Meta:
        model = ProductImage
        fields = [
            "id",
            "product",
            "image",
            "alt_text",
            "is_primary",
            "sort_order",
            "default_image",
            "srcset",
        ]
        read_only_fields = ["id", "default_image", "srcset"]

    def get_default_image(self, obj):
        """Return the preferred processed variant URL for the image.

        Args:
            obj (ProductImage): the image instance.

        Returns:
            str | None: the display URL, or None when unavailable.
        """
        from apps.catalog.images import preferred_image_url

        return preferred_image_url(obj.image.name, obj.image_sources)


class ProductImageWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for images nested under a product.

    ``product`` is set by the view from the URL, not from the request body.
    Creation and update go through a service that keeps a single primary
    image per product.
    """

    class Meta:
        model = ProductImage
        fields = [
            "id",
            "image",
            "alt_text",
            "is_primary",
            "sort_order",
        ]
        read_only_fields = ["id"]

    def validate_image(self, value):
        """Validate the uploaded image via the shared validator."""
        from apps.catalog.validators import validate_image_upload

        validate_image_upload(value)
        return value

    def create(self, validated_data):
        """Create the image, demoting any existing primary first."""
        from apps.catalog.services import create_image

        return create_image(product=validated_data.pop("product"), **validated_data)

    def update(self, instance, validated_data):
        """Update the image, demoting any other primary when promoted."""
        from apps.catalog.services import update_image

        return update_image(instance, **validated_data)


class PricingTierSerializer(serializers.ModelSerializer):
    """Read/write serializer for volume-based pricing tiers."""

    class Meta:
        model = PricingTier
        fields = [
            "id",
            "product",
            "min_quantity",
            "unit_price",
        ]
        read_only_fields = ["id", "product"]


class PricingTierWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for tiers nested under a product.

    ``product`` is set from the URL, not the request body.
    """

    class Meta:
        model = PricingTier
        fields = [
            "id",
            "min_quantity",
            "unit_price",
        ]
        read_only_fields = ["id"]


class RelatedProductSerializer(serializers.ModelSerializer):
    """Read/write serializer for related-product cross-links."""

    class Meta:
        model = RelatedProduct
        fields = [
            "id",
            "product",
            "related_product",
            "relation_type",
            "sort_order",
        ]
        read_only_fields = ["id"]


class RelatedProductWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for related products nested under a product.

    ``product`` is set by the view from the URL; ``related_product`` is
    validated to differ from the parent product and not duplicate an
    existing link.
    """

    class Meta:
        model = RelatedProduct
        fields = [
            "id",
            "related_product",
            "relation_type",
            "sort_order",
        ]
        read_only_fields = ["id"]

    def validate_related_product(self, value):
        """Reject a related-product link where the target matches the parent.

        The parent product is injected into the serializer context by the
        view.

        Returns:
            Product: the validated related product.
        """
        parent_product = self.context.get("parent_product")
        if parent_product and value.pk == parent_product.pk:
            raise serializers.ValidationError("A product cannot be related to itself.")
        return value

    def validate(self, attrs):
        """Reject a link that duplicates an existing product pair.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the product pair is already linked.
        """
        parent_product = self.context.get("parent_product")
        related_product = attrs.get(
            "related_product", getattr(self.instance, "related_product", None)
        )
        if parent_product is not None and related_product is not None:
            queryset = RelatedProduct.objects.filter(
                product=parent_product, related_product=related_product
            )
            if self.instance is not None:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError(
                    {"related_product": "This product link already exists."}
                )
        return attrs


class ProductListSerializer(serializers.ModelSerializer):
    """Slim product representation for list/search endpoints.

    Carries the sellable fields the catalog cards render directly, including
    the server-computed effective price, while omitting heavy nested data.
    """

    category_name = serializers.CharField(
        source="category.name", read_only=True, default=None
    )
    brand_name = serializers.CharField(
        source="brand.name", read_only=True, default=None
    )
    primary_image = serializers.SerializerMethodField()
    effective_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "sku",
            "price",
            "effective_price",
            "compare_at_price",
            "stock_status",
            "short_description",
            "product_type",
            "category",
            "category_name",
            "brand",
            "brand_name",
            "is_active",
            "is_featured",
            "tax_class",
            "average_rating",
            "review_count",
            "primary_image",
            "created_at",
        ]
        read_only_fields = fields

    def get_effective_price(self, obj):
        """Return the server-computed price used by product cards."""
        from apps.promotions.services import get_effective_prices

        prices = self.context.get("effective_prices")
        if prices is None:
            prices = get_effective_prices([obj])
        return prices[obj.pk]["price"]

    def get_primary_image(self, obj):
        """Return the display URL for a product's primary image, or None.

        Reads the ``primary_images`` prefetch added by the list selectors
        and returns the preferred processed variant rather than the
        original upload. When the attribute is absent the product was not
        prefetched (e.g. a detail payload), so a targeted lookup is used
        instead.

        Args:
            obj (Product): the product instance.

        Returns:
            str | None: the primary image display URL, or None.
        """
        from apps.catalog.images import preferred_image_url

        primary = getattr(obj, "primary_images", None)
        if primary is not None:
            if primary:
                try:
                    image = primary[0]
                    return preferred_image_url(image.image.name, image.image_sources)
                except ValueError:
                    return None
            return None
        try:
            primary = obj.images.filter(is_primary=True).first()
        except ValueError:
            return None
        if primary is None:
            return None
        try:
            return preferred_image_url(primary.image.name, primary.image_sources)
        except ValueError:
            return None


class CategorySuggestionSerializer(serializers.ModelSerializer):
    """Minimal category row for the search suggestion dropdown."""

    class Meta:
        model = Category
        fields = ["id", "name", "slug"]
        read_only_fields = fields


class ProductSuggestionSerializer(ProductListSerializer):
    """Slim product row for the search suggestion dropdown.

    Carries only the thumbnail and the server-owned price fields the dropdown
    renders, keeping the per-keystroke response small on metered connections.
    """

    class Meta(ProductListSerializer.Meta):
        fields = [
            "id",
            "name",
            "slug",
            "price",
            "effective_price",
            "compare_at_price",
            "primary_image",
            "brand_name",
        ]
        read_only_fields = fields


class ProductFAQPublicSerializer(serializers.ModelSerializer):
    """Read-only FAQ row shown on the public product detail page."""

    class Meta:
        model = ProductFAQ
        fields = ["id", "question", "answer"]
        read_only_fields = fields


class ProductFAQStaffSerializer(serializers.ModelSerializer):
    """FAQ row returned to the staff editor, including its display position."""

    class Meta:
        model = ProductFAQ
        fields = ["id", "question", "answer", "position"]
        read_only_fields = fields


class ProductFAQListSerializer(serializers.Serializer):
    """Envelope returned by the staff FAQ endpoint."""

    faqs = ProductFAQStaffSerializer(many=True)


class ProductFAQWriteSerializer(serializers.Serializer):
    """One editable FAQ row in a staff replace request.

    Both text fields are optional so a blank editor row can be submitted and
    ignored rather than failing validation. ``id`` is present for a row that
    already exists; omitting it creates a new row.
    """

    id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    question = serializers.CharField(
        max_length=ProductFAQ._meta.get_field("question").max_length,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    answer = serializers.CharField(
        max_length=ProductFAQ._meta.get_field("answer").max_length,
        required=False,
        allow_blank=True,
        allow_null=True,
    )


class ProductFAQReplaceSerializer(serializers.Serializer):
    """Validated full replacement of a product's ordered FAQ list."""

    faqs = ProductFAQWriteSerializer(many=True, allow_empty=True)

    def validate_faqs(self, rows):
        """Normalize rows and raise row-indexed errors for invalid content.

        Args:
            rows (list[dict]): the submitted FAQ rows.

        Returns:
            list[dict]: normalized rows with compact positions.

        Raises:
            serializers.ValidationError: when any row fails validation.
        """
        from apps.catalog.services import validate_product_faq_rows

        cleaned_rows, errors = validate_product_faq_rows(
            rows, product=self.context.get("product")
        )
        if errors:
            raise serializers.ValidationError(errors)
        return cleaned_rows


class ProductDetailCategorySerializer(serializers.ModelSerializer):
    """Category fields for product-detail nesting.

    Deliberately omits ``product_count``: the detail payload does not need
    it and computing it would fire a count query per nested object.
    """

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "image", "is_active", "parent"]
        read_only_fields = fields


class ProductDetailBrandSerializer(serializers.ModelSerializer):
    """Brand fields for product-detail nesting.

    Deliberately omits ``product_count`` for the same reason as
    ``ProductDetailCategorySerializer``.
    """

    class Meta:
        model = Brand
        fields = ["id", "name", "slug", "logo", "description", "is_authorized_dealer"]
        read_only_fields = fields


class RelatedProductDetailSerializer(serializers.ModelSerializer):
    """Slim representation of a related product for inline display.

    Embeds just enough data for the storefront to render related-product
    cards without a separate API call: name, slug, image, price, and the
    relation type. The effective price is computed server-side so the
    storefront never shows a stale or discount-ignoring amount.
    """

    name = serializers.CharField(source="related_product.name", read_only=True)
    slug = serializers.CharField(source="related_product.slug", read_only=True)
    price = serializers.DecimalField(
        source="related_product.price",
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )
    stock_status = serializers.CharField(
        source="related_product.stock_status", read_only=True
    )
    primary_image = serializers.SerializerMethodField()
    effective_price = serializers.SerializerMethodField()

    class Meta:
        model = RelatedProduct
        fields = [
            "id",
            "relation_type",
            "sort_order",
            "name",
            "slug",
            "price",
            "stock_status",
            "primary_image",
            "effective_price",
        ]
        read_only_fields = fields

    def get_primary_image(self, obj):
        """Return the display URL for the related product's primary image.

        Args:
            obj (RelatedProduct): the related-product link.

        Returns:
            str | None: the primary image display URL, or None.
        """
        from apps.catalog.images import preferred_image_url

        primary = getattr(obj.related_product, "primary_images", None)
        if primary:
            try:
                image = primary[0]
                return preferred_image_url(image.image.name, image.image_sources)
            except ValueError, IndexError:
                return None
        return None

    def get_effective_price(self, obj):
        """Return the server-computed effective price for the related product.

        Args:
            obj (RelatedProduct): the related-product link.

        Returns:
            str: the effective price as a decimal string.
        """
        prices = self.context.get("related_prices", {})
        entry = prices.get(obj.related_product_id)
        if entry:
            return entry["price"]
        from apps.promotions.services import get_effective_prices

        return get_effective_prices([obj.related_product])[obj.related_product_id][
            "price"
        ]


class ProductDetailSerializer(serializers.ModelSerializer):
    """Full product representation with nested pricing tiers, images, and related products.

    Includes related products inline to avoid separate API calls during page
    render. The storefront renders related-product cards directly from this
    payload without additional requests.
    """

    category = ProductDetailCategorySerializer(read_only=True)
    brand = ProductDetailBrandSerializer(read_only=True)
    pricing_tiers = PricingTierSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    faqs = ProductFAQPublicSerializer(many=True, read_only=True)
    related_products = serializers.SerializerMethodField()
    replacement_product_name = serializers.CharField(
        source="replacement_product.name", read_only=True, default=None
    )
    effective_pricing = serializers.SerializerMethodField()
    json_ld = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "product_type",
            "category",
            "brand",
            "name",
            "slug",
            "sku",
            "supplier_sku",
            "price",
            "effective_pricing",
            "compare_at_price",
            "cost_price",
            "barcode",
            "weight",
            "dimensions",
            "package_weight",
            "package_dimensions",
            "pieces_per_unit",
            "stock_status",
            "expected_restock_date",
            "short_description",
            "description",
            "specs",
            "features",
            "is_active",
            "is_featured",
            "kebs_certification_number",
            "country_of_origin",
            "hs_code",
            "voltage_rating",
            "frequency_rating",
            "wattage",
            "manufacturer_model_number",
            "manual_pdf",
            "installation_guide_pdf",
            "datasheet_pdf",
            "warranty_duration_months",
            "warranty_type",
            "warranty_provider",
            "warranty_terms",
            "requires_professional_installation",
            "requires_two_person_delivery",
            "is_fragile",
            "condition",
            "is_discontinued",
            "replacement_product",
            "replacement_product_name",
            "last_restocked_at",
            "is_returnable",
            "restocking_fee_percent",
            "tax_class",
            "average_rating",
            "review_count",
            "meta_title",
            "meta_description",
            "gtin",
            "pricing_tiers",
            "images",
            "faqs",
            "related_products",
            "json_ld",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "average_rating",
            "review_count",
            "created_at",
            "updated_at",
        ]

    def get_effective_pricing(self, obj):
        """Return the effective price and discount for the product detail."""
        from apps.promotions.services import get_effective_prices

        return get_effective_prices([obj])[obj.pk]

    def get_related_products(self, obj):
        """Return related products inline, pre-computed with effective prices.

        Uses the prefetched ``related_from`` links to avoid extra queries.

        Args:
            obj (Product): the product instance.

        Returns:
            list[dict]: the serialized related product cards.
        """
        from apps.promotions.services import get_effective_prices

        links = list(obj.related_from.all())
        if not links:
            return []
        related = [link.related_product for link in links]
        prices = get_effective_prices(related)
        return RelatedProductDetailSerializer(
            links,
            many=True,
            context={**self.context, "related_prices": prices},
        ).data

    def get_json_ld(self, obj):
        """Return a schema.org Product JSON-LD object for SEO and crawlers.

        Includes priceCurrency KES and availability, so the storefront can
        inject this directly into a ``<script type="application/ld+json">``
        tag without computing it client-side.

        Args:
            obj (Product): the product instance.

        Returns:
            dict: the JSON-LD structured data.
        """
        from apps.promotions.services import get_effective_prices

        pricing = get_effective_prices([obj])[obj.pk]
        effective_price = pricing["price"]

        stock_map = {
            "in_stock": "https://schema.org/InStock",
            "out_of_stock": "https://schema.org/OutOfStock",
            "pre_order": "https://schema.org/PreOrder",
            "backorder": "https://schema.org/BackOrder",
            "discontinued": "https://schema.org/Discontinued",
        }
        availability = stock_map.get(obj.stock_status, "https://schema.org/InStock")

        ld = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": obj.name,
            "sku": obj.sku,
            "description": obj.short_description or obj.description or "",
            "offers": {
                "@type": "Offer",
                "priceCurrency": "KES",
                "price": str(effective_price),
                "availability": availability,
            },
        }

        if obj.brand:
            ld["brand"] = {"@type": "Brand", "name": obj.brand.name}

        if obj.gtin:
            ld["gtin"] = obj.gtin

        images = list(obj.images.all())
        if images:
            from apps.catalog.images import preferred_image_url

            image_urls = []
            for img in images:
                try:
                    url = preferred_image_url(img.image.name, img.image_sources)
                    if url:
                        image_urls.append(url)
                except ValueError:
                    continue
            if image_urls:
                ld["image"] = image_urls

        if obj.average_rating and obj.review_count:
            ld["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": str(obj.average_rating),
                "reviewCount": obj.review_count,
            }

        return ld


class ProductWriteSerializer(serializers.ModelSerializer):
    """Writable fields for creating/updating a product (admin only).

    ``slug`` is optional on create  -  if omitted it is auto-generated from
    ``name``.
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
        max_length=Product._meta.get_field("slug").max_length,
        validators=[
            UniqueValidator(
                queryset=Product.objects.all(),
                message="A product with this slug already exists.",
            )
        ],
    )
    sku = serializers.CharField(
        max_length=Product._meta.get_field("sku").max_length,
        validators=[
            UniqueValidator(
                queryset=Product.objects.all(),
                message="A product with this SKU already exists.",
            )
        ],
    )

    def validate_manual_pdf(self, value):
        """Validate the manual PDF upload."""
        from apps.catalog.validators import validate_pdf_upload

        validate_pdf_upload(value)
        return value

    def validate_installation_guide_pdf(self, value):
        """Validate the installation guide PDF upload."""
        from apps.catalog.validators import validate_pdf_upload

        validate_pdf_upload(value)
        return value

    def validate_datasheet_pdf(self, value):
        """Validate the datasheet PDF upload."""
        from apps.catalog.validators import validate_pdf_upload

        validate_pdf_upload(value)
        return value

    class Meta:
        model = Product
        fields = [
            "id",
            "product_type",
            "category",
            "brand",
            "name",
            "slug",
            "sku",
            "supplier_sku",
            "price",
            "compare_at_price",
            "cost_price",
            "barcode",
            "weight",
            "dimensions",
            "package_weight",
            "package_dimensions",
            "pieces_per_unit",
            "stock_status",
            "expected_restock_date",
            "short_description",
            "description",
            "specs",
            "features",
            "is_active",
            "is_featured",
            "kebs_certification_number",
            "country_of_origin",
            "hs_code",
            "voltage_rating",
            "frequency_rating",
            "wattage",
            "manufacturer_model_number",
            "manual_pdf",
            "installation_guide_pdf",
            "datasheet_pdf",
            "warranty_duration_months",
            "warranty_type",
            "warranty_provider",
            "warranty_terms",
            "requires_professional_installation",
            "requires_two_person_delivery",
            "is_fragile",
            "condition",
            "is_discontinued",
            "replacement_product",
            "last_restocked_at",
            "is_returnable",
            "restocking_fee_percent",
            "tax_class",
            "meta_title",
            "meta_description",
            "gtin",
        ]
        read_only_fields = ["id"]

    def create(self, validated_data):
        """Create the product, generating a slug from the name if omitted.

        Uses the atomic service so child failures roll back the product.
        """
        from apps.catalog.services import create_product

        return create_product(**validated_data)

    def update(self, instance, validated_data):
        """Update the product, regenerating the slug if the name changes."""
        from apps.catalog.services import update_product

        return update_product(instance, **validated_data)


# FacetDefinition serializers.


class FacetDefinitionSerializer(serializers.ModelSerializer):
    """Read/write serializer for facet definitions.

    Validates that ``key`` or ``field_name`` is set according to the
    ``source_field``, mirroring the model-level ``clean()`` constraint so
    API callers get a clear 400 rather than a silent empty facet at
    computation time.
    """

    class Meta:
        model = FacetDefinition
        fields = [
            "id",
            "name",
            "key",
            "field_name",
            "source_field",
            "facet_type",
            "is_active",
            "sort_order",
        ]
        read_only_fields = ["id"]

    def validate(self, data):
        """Validate the source/field pairing on create and update.

        Args:
            data (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the field pairing is inconsistent.
        """
        source = data.get("source_field", getattr(self.instance, "source_field", None))
        key = data.get("key", getattr(self.instance, "key", ""))
        field_name = data.get("field_name", getattr(self.instance, "field_name", ""))

        json_sources = {"product_specs"}

        if source in json_sources and not key:
            raise serializers.ValidationError(
                {"key": f"'key' is required when source_field is '{source}'."}
            )
        if source == "product_field":
            if not field_name:
                raise serializers.ValidationError(
                    {"field_name": "'field_name' is required for this source."}
                )
            if field_name not in FacetDefinition.FACETABLE_PRODUCT_FIELDS:
                raise serializers.ValidationError(
                    {
                        "field_name": (
                            f"'{field_name}' is not in the allowlist of "
                            "facetable product fields."
                        )
                    }
                )
        return data

    def create(self, validated_data):
        """Create the facet, running the model ``clean()`` as the final check.

        Args:
            validated_data (dict): the validated attributes.

        Returns:
            FacetDefinition: the created facet definition.

        Raises:
            ValidationError: if the model-level clean rules are violated.
        """
        instance = FacetDefinition(**validated_data)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance

    def update(self, instance, validated_data):
        """Update the facet, running the model ``clean()`` as the final check.

        Args:
            instance (FacetDefinition): the facet to update.
            validated_data (dict): the validated attributes.

        Returns:
            FacetDefinition: the updated facet definition.

        Raises:
            ValidationError: if the model-level clean rules are violated.
        """
        for field, value in validated_data.items():
            setattr(instance, field, value)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        instance.save()
        return instance
