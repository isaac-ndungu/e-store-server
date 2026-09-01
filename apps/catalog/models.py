"""Data models for the catalog app.

Defines the product catalog: hierarchical ``Category`` tree, ``Brand`` registry,
``Product`` with JSON specs/features, ``ProductImage`` gallery, ``ProductVariant``
with per-variant pricing, ``RelatedProduct`` cross-links, volume-based
``PricingTier``, and ``FacetDefinition`` metadata that drives the faceted
search endpoint.

Money fields are ``DecimalField`` throughout — never ``float`` — to avoid
rounding errors in price and discount calculations.
"""

from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models

from apps.catalog.validators import validate_image_upload, validate_pdf_upload


class Category(models.Model):
    """A hierarchical product category.

    Categories form a tree via the self-referential ``parent`` FK. Leaf
    categories contain products; parent categories group sub-categories for
    navigation. ``slug`` is unique and used in storefront URLs.
    """

    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    image = models.ImageField(
        upload_to="categories/", blank=True, validators=[validate_image_upload]
    )
    meta_title = models.CharField(max_length=255, blank=True)
    meta_description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["slug"], name="cat_slug_idx"),
            models.Index(fields=["is_active"], name="cat_active_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("pk")),
                name="category_no_self_parent",
            ),
        ]

    def __str__(self):
        return self.name


class Brand(models.Model):
    """A product brand or manufacturer.

    ``is_authorized_dealer`` flags whether the business is an authorized
    dealer for this brand — displayed as a trust signal on the storefront.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    logo = models.ImageField(
        upload_to="brands/", blank=True, validators=[validate_image_upload]
    )
    description = models.TextField(blank=True)
    is_authorized_dealer = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["slug"], name="brand_slug_idx"),
        ]

    def __str__(self):
        return self.name


class Product(models.Model):
    """A sellable product in the catalog.

    Products carry structured ``specs`` (JSON) and ``features`` (JSON) for
    display and faceted search. The ``tax_class`` field determines VAT
    treatment at checkout — a cart can mix standard/zero-rated/exempt products,
    so tax is always computed per line item, never from a single flat rate.

    ``tracks_serial_numbers`` controls whether inventory for this product is
    tracked at the individual serial-unit level (e.g. appliances with
    warranties) or as a simple quantity count. ``last_restocked_at`` drives
    the ``restocked`` smart-collection rule.
    """

    PRODUCT_TYPE_CHOICES = (
        ("physical", "Physical"),
        ("digital", "Digital"),
        ("service", "Service"),
    )
    CONDITION_CHOICES = (
        ("new", "New"),
        ("open_box", "Open Box"),
        ("refurbished", "Refurbished"),
        ("floor_model", "Floor Model"),
    )
    TAX_CLASS_CHOICES = (
        ("standard", "Standard VAT"),
        ("zero_rated", "Zero-rated"),
        ("exempt", "Exempt"),
    )
    WARRANTY_TYPE_CHOICES = (
        ("manufacturer", "Manufacturer"),
        ("dealer", "Dealer/Local"),
    )

    product_type = models.CharField(
        max_length=20, choices=PRODUCT_TYPE_CHOICES, default="physical"
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        related_name="products",
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        related_name="products",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    sku = models.CharField(max_length=100, unique=True)
    short_description = models.CharField(max_length=500, blank=True)
    description = models.TextField()
    specs = models.JSONField(default=dict, blank=True)
    features = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    kebs_certification_number = models.CharField(max_length=100, blank=True)
    country_of_origin = models.CharField(max_length=100, blank=True)
    hs_code = models.CharField(max_length=20, blank=True)
    voltage_rating = models.CharField(max_length=50, blank=True)
    frequency_rating = models.CharField(max_length=20, blank=True)
    wattage = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    manufacturer_model_number = models.CharField(max_length=100, blank=True)
    manual_pdf = models.FileField(
        upload_to="products/manuals/", blank=True, validators=[validate_pdf_upload]
    )
    installation_guide_pdf = models.FileField(
        upload_to="products/guides/", blank=True, validators=[validate_pdf_upload]
    )
    datasheet_pdf = models.FileField(
        upload_to="products/datasheets/", blank=True, validators=[validate_pdf_upload]
    )
    tracks_serial_numbers = models.BooleanField(default=False)
    warranty_duration_months = models.PositiveIntegerField(null=True, blank=True)
    warranty_type = models.CharField(
        max_length=20, choices=WARRANTY_TYPE_CHOICES, blank=True
    )
    warranty_provider = models.CharField(max_length=255, blank=True)
    warranty_terms = models.TextField(blank=True)
    requires_professional_installation = models.BooleanField(default=False)
    requires_two_person_delivery = models.BooleanField(default=False)
    is_fragile = models.BooleanField(default=False)
    condition = models.CharField(
        max_length=20, choices=CONDITION_CHOICES, default="new"
    )
    is_discontinued = models.BooleanField(default=False)
    replacement_product = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="superseded_by",
    )
    last_restocked_at = models.DateTimeField(null=True, blank=True)
    is_returnable = models.BooleanField(default=True)
    restocking_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0
    )
    tax_class = models.CharField(
        max_length=20, choices=TAX_CLASS_CHOICES, default="standard"
    )
    average_rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    review_count = models.PositiveIntegerField(default=0)
    meta_title = models.CharField(max_length=255, blank=True)
    meta_description = models.TextField(blank=True)
    gtin = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["slug"], name="prod_slug_idx"),
            models.Index(fields=["sku"], name="prod_sku_idx"),
            models.Index(fields=["name"], name="prod_name_idx"),
            models.Index(fields=["is_active"], name="prod_active_idx"),
            models.Index(fields=["is_featured"], name="prod_featured_idx"),
            models.Index(fields=["category"], name="prod_category_idx"),
            models.Index(fields=["brand"], name="prod_brand_idx"),
            models.Index(fields=["product_type"], name="prod_ptype_idx"),
            models.Index(fields=["tax_class"], name="prod_taxclass_idx"),
            models.Index(fields=["average_rating"], name="prod_rating_idx"),
            models.Index(fields=["review_count"], name="prod_review_idx"),
            models.Index(fields=["-created_at"], name="prod_created_idx"),
            GinIndex(
                name="prod_specs_gin_idx",
                fields=["specs"],
                opclasses=["jsonb_path_ops"],
            ),
        ]

    def __str__(self):
        return self.name


class ProductImage(models.Model):
    """An image in a product's gallery.

    ``is_primary`` marks the hero/thumbnail image. ``sort_order`` controls
    display sequence. Images are validated on upload for format (JPEG, PNG,
    WebP, AVIF) and size (max 10 MB), then resized/re-encoded into the
    responsive variant set recorded in ``image_sources`` (AVIF/WebP at fixed
    widths). Storefront URLs come from ``image_sources``; the original byte
    stream in ``image`` is not served to browsers.
    """

    product = models.ForeignKey(
        Product, related_name="images", on_delete=models.CASCADE
    )
    image = models.ImageField(
        upload_to="products/images/",
        validators=[validate_image_upload],
    )
    alt_text = models.CharField(max_length=255, blank=True)
    is_primary = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    image_sources = models.JSONField(
        default=list,
        blank=True,
        help_text="Processed variant metadata: width, format, and URL per image.",
    )

    class Meta:
        ordering = ["sort_order"]
        verbose_name_plural = "product images"
        indexes = [
            models.Index(fields=["product", "sort_order"], name="img_prod_sort_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["product"],
                condition=models.Q(is_primary=True),
                name="unique_primary_image_per_product",
            )
        ]

    def __str__(self):
        return f"Image for {self.product_id} ({self.sort_order})"


class ProductVariant(models.Model):
    """A specific variant of a product (e.g. colour, size, capacity).

    Every variant carries its own ``sku``, ``price``, and optional
    ``compare_at_price`` / ``cost_price``. ``attributes`` is a JSON dict
    holding the variant-specific attributes (e.g. ``{"color": "Silver",
    "capacity": "200L"}``). ``package_weight`` and ``package_dimensions``
    are used for volumetric shipping fee calculation.
    """

    product = models.ForeignKey(
        Product, related_name="variants", on_delete=models.CASCADE
    )
    sku = models.CharField(max_length=100, unique=True)
    supplier_sku = models.CharField(max_length=100, blank=True)
    attributes = models.JSONField(default=dict)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    compare_at_price = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True
    )
    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True
    )
    barcode = models.CharField(max_length=100, blank=True)
    weight = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    dimensions = models.JSONField(default=dict, blank=True)
    package_weight = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True
    )
    package_dimensions = models.JSONField(default=dict, blank=True)
    pieces_per_unit = models.PositiveIntegerField(default=1)
    stock_status_text = models.CharField(max_length=100, blank=True)
    expected_restock_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product"], name="var_product_idx"),
            models.Index(fields=["sku"], name="var_sku_idx"),
            models.Index(fields=["is_active"], name="var_active_idx"),
            GinIndex(
                name="var_attrs_gin_idx",
                fields=["attributes"],
                opclasses=["jsonb_path_ops"],
            ),
        ]

    def __str__(self):
        return f"{self.product_id} — {self.sku}"


class RelatedProduct(models.Model):
    """A cross-link between two products (accessory, alternative, upgrade).

    ``relation_type`` drives storefront display labels and filtering.
    ``sort_order`` controls display sequence within a relation type.
    """

    RELATION_TYPE_CHOICES = (
        ("accessory", "Accessory"),
        ("alternative", "Alternative"),
        ("upgrade", "Upgrade"),
    )

    product = models.ForeignKey(
        Product, related_name="related_from", on_delete=models.CASCADE
    )
    related_product = models.ForeignKey(
        Product, related_name="related_to", on_delete=models.CASCADE
    )
    relation_type = models.CharField(max_length=20, choices=RELATION_TYPE_CHOICES)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]
        indexes = [
            models.Index(
                fields=["product", "relation_type"], name="relprod_prod_type_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "related_product"],
                name="unique_related_product_link",
            ),
            models.CheckConstraint(
                condition=~models.Q(product=models.F("related_product")),
                name="related_product_no_self_link",
            ),
        ]

    def __str__(self):
        return f"{self.product_id} → {self.related_product_id} ({self.relation_type})"


class PricingTier(models.Model):
    """Volume-based pricing for a product variant.

    When a customer orders ``min_quantity`` or more of a variant, the
    ``unit_price`` applies. Tiers are evaluated in ascending
    ``min_quantity`` order; the highest qualifying tier wins.
    """

    variant = models.ForeignKey(
        ProductVariant, related_name="pricing_tiers", on_delete=models.CASCADE
    )
    min_quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["min_quantity"]
        indexes = [
            models.Index(fields=["variant", "min_quantity"], name="tier_var_qty_idx"),
        ]

    def __str__(self):
        return f"Tier {self.min_quantity}+ for {self.variant_id}"


class FacetDefinition(models.Model):
    """Metadata describing a filterable facet for the product catalog.

    ``source_field`` determines which underlying data store is queried:
    ``product_specs`` or ``variant_attributes`` for JSON fields, or
    ``product_field`` / ``variant_field`` for direct model columns.

    ``key`` is the JSON key path (required for JSON sources) and
    ``field_name`` is the Django model field name (required for relational
    sources). Both must be consistent with the ``source_field`` — the
    ``clean()`` method enforces this.
    """

    SOURCE_CHOICES = (
        ("product_specs", "Product.specs (JSON)"),
        ("variant_attributes", "ProductVariant.attributes (JSON)"),
        ("product_field", "Direct Product field"),
        ("variant_field", "Direct ProductVariant field"),
    )
    FACET_TYPE_CHOICES = (
        ("choice", "Choice"),
        ("range", "Range"),
    )

    name = models.CharField(max_length=100)
    key = models.CharField(
        max_length=100,
        blank=True,
        help_text="JSON key path — required when source is product_specs or variant_attributes.",
    )
    field_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Model field name — required when source is product_field or variant_field.",
    )
    source_field = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    facet_type = models.CharField(
        max_length=20, choices=FACET_TYPE_CHOICES, default="choice"
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["is_active"], name="facet_active_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_source_field_display()})"

    def clean(self):
        """Validate that ``key`` / ``field_name`` are set per the source type.

        Raises:
            ValidationError: if the field pairing is inconsistent.
        """
        super().clean()
        json_sources = {"product_specs", "variant_attributes"}
        field_sources = {"product_field", "variant_field"}

        if self.source_field in json_sources and not self.key:
            raise ValidationError(
                {
                    "key": (
                        f"'key' is required when source_field is "
                        f"'{self.source_field}'."
                    )
                }
            )
        if self.source_field in field_sources and not self.field_name:
            raise ValidationError(
                {
                    "field_name": (
                        f"'field_name' is required when source_field is "
                        f"'{self.source_field}'."
                    )
                }
            )
