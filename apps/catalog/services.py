"""Business logic for the catalog app.

CRUD operations, slug generation, facet aggregation, and query-param
validation for the faceted product search. Views stay thin — all write
logic and non-trivial read computation lives here.

Facet aggregation runs one aggregate query per active ``FacetDefinition``
per request. This is an intentional trade-off: it keeps the implementation
straightforward and avoids premature caching before the project's Redis
cache invalidation pattern is established for other read-heavy data. A
future improvement can add Redis-backed caching for facet results with
signal-based invalidation.
"""

import re
import unicodedata
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

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

# Maximum number of active facets allowed to bound query cost.
MAX_ACTIVE_FACETS = 15


# Slug generation


def generate_unique_slug(instance, value, slug_field="slug"):
    """Generate a URL-safe unique slug from ``value``.

    Strips accents, lowercases, replaces non-alphanumeric characters with
    hyphens, and appends a numeric suffix if the slug already exists.

    Args:
        instance: the model instance (excluded from uniqueness check on
            update).
        value (str): the source text (typically the name).
        slug_field (str): the field name to check uniqueness against.

    Returns:
        str: a unique slug.
    """
    slug = slugify(unicodedata.normalize("NFKD", value))
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    slug = slug.lower()

    base_slug = slug
    counter = 1
    queryset = instance.__class__._default_manager.all()
    if instance.pk:
        queryset = queryset.exclude(pk=instance.pk)

    while queryset.filter(**{slug_field: slug}).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    return slug


# Category CRUD


def create_category(*, name, slug=None, **kwargs):
    """Create and return a new category.

    Auto-generates a slug from the name if not provided.

    Args:
        name (str): the category name.
        slug (str | None): explicit slug, or None to auto-generate.
        **kwargs: additional Category fields.

    Returns:
        Category: the newly created category.
    """
    if not slug:
        slug = generate_unique_slug(Category(name=name), name)
    return Category.objects.create(name=name, slug=slug, **kwargs)


def update_category(category, **data):
    """Update a category's fields.

    Auto-generates a slug from the name if the name changes and no explicit
    slug is provided.

    Args:
        category (Category): the category to update.
        **data: fields to update.

    Returns:
        Category: the updated category.
    """
    if "name" in data and "slug" not in data:
        data["slug"] = generate_unique_slug(category, data["name"])
    for field, value in data.items():
        setattr(category, field, value)
    category.save()
    return category


# Brand CRUD


def create_brand(*, name, slug=None, **kwargs):
    """Create and return a new brand.

    Args:
        name (str): the brand name.
        slug (str | None): explicit slug, or None to auto-generate.
        **kwargs: additional Brand fields.

    Returns:
        Brand: the newly created brand.
    """
    if not slug:
        slug = generate_unique_slug(Brand(name=name), name)
    return Brand.objects.create(name=name, slug=slug, **kwargs)


def update_brand(brand, **data):
    """Update a brand's fields.

    Args:
        brand (Brand): the brand to update.
        **data: fields to update.

    Returns:
        Brand: the updated brand.
    """
    if "name" in data and "slug" not in data:
        data["slug"] = generate_unique_slug(brand, data["name"])
    for field, value in data.items():
        setattr(brand, field, value)
    brand.save()
    return brand


# Product CRUD (atomic multi-model creation)


def create_product(
    *,
    name,
    slug=None,
    sku,
    variants=None,
    images=None,
    related_products=None,
    pricing_tiers=None,
    **kwargs,
):
    """Create a product with its nested children atomically.

    Wraps the full creation in ``transaction.atomic()`` so that a failure
    partway through (e.g. a variant with a duplicate SKU) rolls back the
    product and any already-created children, preventing orphaned rows.

    Args:
        name (str): the product name.
        slug (str | None): explicit slug, or None to auto-generate.
        sku (str): the product-level SKU (must be unique).
        variants (list[dict] | None): variant data dicts to create.
        images (list[dict] | None): image data dicts to create.
        related_products (list[dict] | None): related-product data dicts.
        pricing_tiers (list[dict] | None): pricing tier data dicts.
        **kwargs: additional Product fields.

    Returns:
        Product: the newly created product with nested children.

    Raises:
        ValidationError: if a related product links to its parent.
        IntegrityError: if a uniqueness constraint is violated.
    """
    if not slug:
        slug = generate_unique_slug(Product(name=name), name)

    with transaction.atomic():
        product = Product.objects.create(name=name, slug=slug, sku=sku, **kwargs)

        if variants:
            for variant_data in variants:
                ProductVariant.objects.create(product=product, **variant_data)

        if images:
            primary_claimed = False
            for image_data in images:
                if image_data.get("is_primary"):
                    if primary_claimed:
                        image_data = {**image_data, "is_primary": False}
                    else:
                        primary_claimed = True
                image = ProductImage.objects.create(product=product, **image_data)
                enqueue_image_variants(image)

        if related_products:
            linked_products = set()
            for rp_data in related_products:
                related = rp_data.get("related_product")
                related_pk = related.pk if isinstance(related, Product) else related
                if related_pk == product.pk:
                    raise ValidationError("A product cannot be related to itself.")
                if related_pk in linked_products:
                    continue
                linked_products.add(related_pk)
                RelatedProduct.objects.create(product=product, **rp_data)

        if pricing_tiers:
            for tier_data in pricing_tiers:
                PricingTier.objects.create(**tier_data)

    return product


def update_product(product, **data):
    """Update a product's fields.

    Args:
        product (Product): the product to update.
        **data: fields to update.

    Returns:
        Product: the updated product.
    """
    if "name" in data and "slug" not in data:
        data["slug"] = generate_unique_slug(product, data["name"])
    for field, value in data.items():
        setattr(product, field, value)
    product.save()
    return product


# Facet aggregation


def get_active_facets():
    """Return active ``FacetDefinition`` rows ordered by sort order.

    Caps the result at ``MAX_ACTIVE_FACETS`` to bound query cost.

    Returns:
        QuerySet: active facet definitions.
    """
    return FacetDefinition.objects.filter(is_active=True).order_by(
        "sort_order", "name"
    )[:MAX_ACTIVE_FACETS]


def compute_facet_counts(queryset):
    """Compute aggregate counts for each active facet against the queryset.

    Each choice facet runs a single grouped aggregate query rather than a
    count query per distinct value. Range facets run a single aggregate
    query returning their min/max bounds. The result is a dict keyed by
    facet ``name``: choice facets map to ``{facet_value: count}``, range
    facets to ``{"min": ..., "max": ...}``.

    Note: This still runs one query per active facet. Redis caching of
    these results is deferred to when the project's cache invalidation
    pattern is established.

    Args:
        queryset (QuerySet): the base product queryset (already filtered by
            any non-facet filters like category/brand).

    Returns:
        dict: ``{"facet_name": {value/count or min/max}, ...}``
    """
    facets = get_active_facets()
    result = {}

    for facet in facets:
        if facet.facet_type == "range":
            result[facet.name] = _range_stats_for_facet(queryset, facet)
        else:
            result[facet.name] = _choice_counts_for_facet(queryset, facet)

    return result


def _choice_counts_for_facet(queryset, facet):
    """Group counts per distinct value for a choice facet.

    Args:
        queryset (QuerySet): the filtered product queryset.
        facet (FacetDefinition): the choice facet definition.

    Returns:
        dict: ``{value: count}`` for each distinct facet value.
    """
    from django.db.models import Count

    counts = {}
    if facet.source_field == "product_specs" and facet.key:
        rows = (
            queryset.exclude(**{f"specs__{facet.key}": None})
            .values(f"specs__{facet.key}")
            .annotate(count=Count("id"))
            .values_list(f"specs__{facet.key}", "count")
        )
        for value, count in rows:
            if value is not None:
                counts[str(value)] = count

    elif facet.source_field == "variant_attributes" and facet.key:
        rows = (
            ProductVariant.objects.filter(product__in=queryset)
            .exclude(**{f"attributes__{facet.key}": None})
            .values(f"attributes__{facet.key}")
            .annotate(count=Count("id"))
            .values_list(f"attributes__{facet.key}", "count")
        )
        for value, count in rows:
            if value is not None:
                counts[str(value)] = count

    elif facet.source_field == "product_field" and facet.field_name:
        if hasattr(Product, facet.field_name):
            rows = (
                queryset.exclude(**{facet.field_name: None})
                .values(facet.field_name)
                .annotate(count=Count("id"))
                .values_list(facet.field_name, "count")
            )
            for value, count in rows:
                if value not in (None, ""):
                    counts[str(value)] = count

    elif facet.source_field == "variant_field" and facet.field_name:
        if hasattr(ProductVariant, facet.field_name):
            rows = (
                ProductVariant.objects.filter(product__in=queryset)
                .exclude(**{facet.field_name: None})
                .values(facet.field_name)
                .annotate(count=Count("id"))
                .values_list(facet.field_name, "count")
            )
            for value, count in rows:
                if value not in (None, ""):
                    counts[str(value)] = count

    return counts


def _range_stats_for_facet(queryset, facet):
    """Compute inclusive min/max bounds for a range facet.

    Fetches the distinct values for the facet and computes the numeric
    min/max in Python. Working from the per-value rows keeps the behavior
    identical across database backends, where JSON column aggregation can
    report raw JSON types or trigger backend-specific conversion quirks.

    Args:
        queryset (QuerySet): the filtered product queryset.
        facet (FacetDefinition): the range facet definition.

    Returns:
        dict: ``{"min": ..., "max": ...}`` or ``{}`` when no data exists.
    """
    values = []
    if facet.source_field == "product_specs" and facet.key:
        values = (
            queryset.exclude(**{f"specs__{facet.key}": None})
            .values_list(f"specs__{facet.key}", flat=True)
            .distinct()
        )
    elif facet.source_field == "variant_attributes" and facet.key:
        values = (
            ProductVariant.objects.filter(product__in=queryset)
            .exclude(**{f"attributes__{facet.key}": None})
            .values_list(f"attributes__{facet.key}", flat=True)
            .distinct()
        )
    elif facet.source_field == "product_field" and facet.field_name:
        if hasattr(Product, facet.field_name):
            values = (
                queryset.exclude(**{facet.field_name: None})
                .values_list(facet.field_name, flat=True)
                .distinct()
            )
    elif facet.source_field == "variant_field" and facet.field_name:
        if hasattr(ProductVariant, facet.field_name):
            values = (
                ProductVariant.objects.filter(product__in=queryset)
                .exclude(**{facet.field_name: None})
                .values_list(facet.field_name, flat=True)
                .distinct()
            )

    numeric = [
        value
        for value in values
        if value is not None and value != "" and _coerce_numeric(value) is not None
    ]
    if not numeric:
        return {}

    converted = [Decimal(value) for value in numeric]

    if converted:
        return {"min": str(min(converted)), "max": str(max(converted))}
    return {}


# Facet query param validation


def _coerce_numeric(value):
    """Return a numeric form of ``value`` if it looks numeric, else None.

    Args:
        value (str): the raw query-param value.

    Returns:
        int | float | None: the coerced number when the value is numeric.
    """
    try:
        return int(value)
    except TypeError, ValueError:
        pass
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def validate_facet_params(query_params):
    """Build a Q filter from valid facet params against active facets.

    Choice facets match the param with an exact lookup. Because JSONB is
    type sensitive — an integer ``200`` stored in ``specs`` is not equal to
    the string ``"200"`` — numeric-looking values also match their typed
    equivalent. Range facets accept ``<key>_min`` / ``<key>_max`` params and
    apply ``__gte`` / ``__lte`` lookups. Unknown params are silently
    ignored.

    Args:
        query_params (QueryDict): the request query parameters.

    Returns:
        Q: the combined filter (an empty Q when no facet params apply).
    """
    facets = get_active_facets()
    query = Q()

    for facet in facets:
        if facet.source_field == "product_specs" and facet.key:
            param_key, lookup = facet.key, f"specs__{facet.key}"
        elif facet.source_field == "variant_attributes" and facet.key:
            param_key, lookup = facet.key, f"variants__attributes__{facet.key}"
        elif facet.source_field == "product_field" and facet.field_name:
            param_key, lookup = facet.field_name, facet.field_name
        elif facet.source_field == "variant_field" and facet.field_name:
            param_key, lookup = facet.field_name, f"variants__{facet.field_name}"
        else:
            continue

        if facet.facet_type == "range":
            min_value = query_params.get(f"{param_key}_min")
            max_value = query_params.get(f"{param_key}_max")
            if min_value not in (None, ""):
                parsed_min = _coerce_numeric(min_value)
                query &= Q(
                    **{
                        f"{lookup}__gte": (
                            min_value if parsed_min is None else parsed_min
                        )
                    }
                )
            if max_value not in (None, ""):
                parsed_max = _coerce_numeric(max_value)
                query &= Q(
                    **{
                        f"{lookup}__lte": (
                            max_value if parsed_max is None else parsed_max
                        )
                    }
                )
            continue

        value = query_params.get(param_key)
        if value is None or value == "":
            continue
        coerced = _coerce_numeric(value)
        if coerced is None:
            query &= Q(**{lookup: value})
        else:
            query &= Q(**{lookup: value}) | Q(**{lookup: coerced})

    return query


# Product image lifecycle


def enqueue_image_variants(product_image):
    """Queue background generation of responsive variants for an image.

    Args:
        product_image (ProductImage): the image to process.
    """
    from apps.catalog.tasks import generate_product_image_variants

    generate_product_image_variants.delay(product_image.pk, product_image.image.name)


def create_image(product, *, image, alt_text="", is_primary=False, sort_order=0):
    """Create a product image, keeping at most one primary per product.

    Setting ``is_primary`` demotes any existing primary image for the
    product first so the uniqueness constraint never fires. Variant
    generation is queued after the row exists.

    Args:
        product (Product): the parent product.
        image: the uploaded image file.
        alt_text (str): alternate text for the image.
        is_primary (bool): whether this image becomes the primary.
        sort_order (int): display ordering.

    Returns:
        ProductImage: the created image.
    """
    with transaction.atomic():
        if is_primary:
            ProductImage.objects.filter(product=product, is_primary=True).update(
                is_primary=False
            )
        image_obj = ProductImage.objects.create(
            product=product,
            image=image,
            alt_text=alt_text,
            is_primary=is_primary,
            sort_order=sort_order,
        )
    enqueue_image_variants(image_obj)
    return image_obj


def update_image(image, **data):
    """Update a product image, keeping at most one primary per product.

    Replacing the uploaded file discards previously generated variants and
    queues regeneration.

    Args:
        image (ProductImage): the image to update.
        **data: fields to update.

    Returns:
        ProductImage: the updated image.
    """
    replace_file = "image" in data
    with transaction.atomic():
        if data.get("is_primary"):
            ProductImage.objects.filter(
                product_id=image.product_id, is_primary=True
            ).exclude(pk=image.pk).update(is_primary=False)
        if replace_file:
            image.image_sources = []
        for field, value in data.items():
            setattr(image, field, value)
        image.save()
    if replace_file:
        enqueue_image_variants(image)
    return image
