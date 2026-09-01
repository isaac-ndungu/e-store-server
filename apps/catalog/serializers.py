from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

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

    ``slug`` is optional on create — if omitted it is auto-generated from
    ``name``.
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
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

    ``slug`` is optional on create — if omitted it is auto-generated from
    ``name``.
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
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
    lists the responsive variants (width + format + URL) generated from the
    upload. Browsers pick from ``srcset``; the original upload is not meant
    to be served to customers.
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
            "variant",
            "min_quantity",
            "unit_price",
        ]
        read_only_fields = ["id"]


class PricingTierWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for tiers nested under a product.

    ``variant`` is validated to belong to the product scoping the request when
    the view supplies the parent product in the serializer context.
    """

    class Meta:
        model = PricingTier
        fields = [
            "id",
            "variant",
            "min_quantity",
            "unit_price",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        """Check the variant belongs to the product scoping the request.

        Args:
            attrs (dict): the validated data (partial on PATCH).

        Returns:
            dict: the validated data.

        Raises:
            ValidationError: if the variant belongs to another product.
        """
        parent_product = self.context.get("parent_product")
        variant = attrs.get("variant", getattr(self.instance, "variant", None))
        if (
            parent_product is not None
            and variant is not None
            and variant.product_id != parent_product.pk
        ):
            raise serializers.ValidationError(
                {"variant": "This variant does not belong to the specified product."}
            )
        return attrs


class ProductVariantListSerializer(serializers.ModelSerializer):
    """Slim variant representation for product list endpoints."""

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "sku",
            "attributes",
            "price",
            "compare_at_price",
            "is_active",
        ]
        read_only_fields = ["id", "sku", "attributes", "price", "compare_at_price"]


class ProductVariantDetailSerializer(serializers.ModelSerializer):
    """Full variant representation including pricing tiers."""

    pricing_tiers = PricingTierSerializer(many=True, read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "product",
            "sku",
            "supplier_sku",
            "attributes",
            "price",
            "compare_at_price",
            "cost_price",
            "barcode",
            "weight",
            "dimensions",
            "package_weight",
            "package_dimensions",
            "pieces_per_unit",
            "stock_status_text",
            "expected_restock_date",
            "is_active",
            "pricing_tiers",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ProductVariantWriteSerializer(serializers.ModelSerializer):
    """Writable fields for creating/updating a variant (admin only)."""

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "sku",
            "supplier_sku",
            "attributes",
            "price",
            "compare_at_price",
            "cost_price",
            "barcode",
            "weight",
            "dimensions",
            "package_weight",
            "package_dimensions",
            "pieces_per_unit",
            "stock_status_text",
            "expected_restock_date",
            "is_active",
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

    Omits heavy nested data (variants, images) to keep list payloads small.
    """

    category_name = serializers.CharField(
        source="category.name", read_only=True, default=None
    )
    brand_name = serializers.CharField(
        source="brand.name", read_only=True, default=None
    )
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "sku",
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


class ProductDetailSerializer(serializers.ModelSerializer):
    """Full product representation with nested variants and images."""

    category = ProductDetailCategorySerializer(read_only=True)
    brand = ProductDetailBrandSerializer(read_only=True)
    variants = ProductVariantListSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    replacement_product_name = serializers.CharField(
        source="replacement_product.name", read_only=True, default=None
    )

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
            "tracks_serial_numbers",
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
            "variants",
            "images",
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


class ProductWriteSerializer(serializers.ModelSerializer):
    """Writable fields for creating/updating a product (admin only).

    ``slug`` is optional on create — if omitted it is auto-generated from
    ``name``.
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
        validators=[
            UniqueValidator(
                queryset=Product.objects.all(),
                message="A product with this slug already exists.",
            )
        ],
    )
    sku = serializers.CharField(
        validators=[
            UniqueValidator(
                queryset=Product.objects.all(),
                message="A product with this SKU already exists.",
            )
        ]
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
            "tracks_serial_numbers",
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

        json_sources = {"product_specs", "variant_attributes"}
        field_sources = {"product_field", "variant_field"}

        if source in json_sources and not key:
            raise serializers.ValidationError(
                {"key": f"'key' is required when source_field is '{source}'."}
            )
        if source in field_sources and not field_name:
            raise serializers.ValidationError(
                {
                    "field_name": (
                        f"'field_name' is required when source_field is " f"'{source}'."
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
