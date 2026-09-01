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

    product_count = serializers.IntegerField(source="products.count", read_only=True)

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


class CategoryDetailSerializer(serializers.ModelSerializer):
    """Full category representation including sub-categories."""

    children = CategoryListSerializer(many=True, read_only=True)
    product_count = serializers.IntegerField(source="products.count", read_only=True)

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

    product_count = serializers.IntegerField(source="products.count", read_only=True)

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


class BrandDetailSerializer(serializers.ModelSerializer):
    """Full brand representation."""

    product_count = serializers.IntegerField(source="products.count", read_only=True)

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
    """Read/write serializer for product images."""

    class Meta:
        model = ProductImage
        fields = [
            "id",
            "product",
            "image",
            "alt_text",
            "is_primary",
            "sort_order",
        ]
        read_only_fields = ["id"]


class ProductImageWriteSerializer(serializers.ModelSerializer):
    """Writable serializer for images nested under a product.

    ``product`` is set by the view from the URL, not from the request body.
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

    ``variant`` is validated by the view to ensure it belongs to the product
    in the URL.
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
    validated to differ from the parent product.
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
        """
        parent_product = self.context.get("parent_product")
        if parent_product and value.pk == parent_product.pk:
            raise serializers.ValidationError("A product cannot be related to itself.")
        return value


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
        """Return the primary image URL for a product, or None.

        Args:
            obj (Product): the product instance.

        Returns:
            str | None: the primary image URL, or None.
        """
        if not hasattr(obj, "_prefetched_images_cache"):
            return None
        primary = [img for img in obj._prefetched_images_cache if img.is_primary]
        if primary:
            try:
                return primary[0].image.url
            except ValueError:
                return None
        return None


class ProductDetailSerializer(serializers.ModelSerializer):
    """Full product representation with nested variants and images."""

    category = CategoryListSerializer(read_only=True)
    brand = BrandListSerializer(read_only=True)
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
