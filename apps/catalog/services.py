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

from django.db import transaction
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
        ValidationError: if a child resource fails validation.
        IntegrityError: if a uniqueness constraint is violated (caught and
            re-raised as ValidationError).
    """
    if not slug:
        slug = generate_unique_slug(Product(name=name), name)

    with transaction.atomic():
        product = Product.objects.create(name=name, slug=slug, sku=sku, **kwargs)

        if variants:
            for variant_data in variants:
                ProductVariant.objects.create(product=product, **variant_data)

        if images:
            for image_data in images:
                ProductImage.objects.create(product=product, **image_data)

        if related_products:
            for rp_data in related_products:
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

    Each facet runs one aggregate query. The result is a dict keyed by facet
    ``name`` with values as ``{facet_value: count}`` dicts. For JSON-source
    facets, the aggregate uses a ``__contains`` lookup; for relational-source
    facets, a standard field filter.

    Note: This currently runs one query per active facet. Redis caching of
    these results is deferred to when the project's cache invalidation
    pattern is established.

    Args:
        queryset (QuerySet): the base product queryset (already filtered by
            any non-facet filters like category/brand).

    Returns:
        dict: ``{"facet_name": {"value": count, ...}, ...}``
    """
    facets = get_active_facets()
    result = {}

    for facet in facets:
        counts = {}
        if facet.source_field == "product_specs" and facet.key:
            qs = queryset.filter(**{f"specs__{facet.key}__isnull": False})
            values = qs.values_list(f"specs__{facet.key}", flat=True).distinct()
            for val in values:
                if val is not None:
                    count = qs.filter(**{f"specs__{facet.key}": val}).count()
                    counts[str(val)] = count

        elif facet.source_field == "variant_attributes" and facet.key:
            qs = ProductVariant.objects.filter(
                product__in=queryset,
                **{f"attributes__{facet.key}__isnull": False},
            )
            values = qs.values_list(f"attributes__{facet.key}", flat=True).distinct()
            for val in values:
                if val is not None:
                    count = qs.filter(**{f"attributes__{facet.key}": val}).count()
                    counts[str(val)] = count

        elif facet.source_field == "product_field" and facet.field_name:
            if hasattr(Product, facet.field_name):
                qs = queryset.exclude(**{facet.field_name: ""}).exclude(
                    **{facet.field_name: None}
                )
                values = qs.values_list(facet.field_name, flat=True).distinct()
                for val in values:
                    if val is not None:
                        count = qs.filter(**{facet.field_name: val}).count()
                        counts[str(val)] = count

        elif facet.source_field == "variant_field" and facet.field_name:
            if hasattr(ProductVariant, facet.field_name):
                qs = (
                    ProductVariant.objects.filter(
                        product__in=queryset,
                    )
                    .exclude(**{facet.field_name: ""})
                    .exclude(**{facet.field_name: None})
                )
                values = qs.values_list(facet.field_name, flat=True).distinct()
                for val in values:
                    if val is not None:
                        count = qs.filter(**{facet.field_name: val}).count()
                        counts[str(val)] = count

        result[facet.name] = counts

    return result


# Facet query param validation


def validate_facet_params(query_params):
    """Validate facet-related query params against active ``FacetDefinition`` rows.

    Returns a dict of ``{field_lookup: value}`` for params that match an
    active facet, silently ignoring unknown params. The caller applies
    these lookups to the product queryset.

    For JSON-source facets the lookup is ``specs__<key>`` or
    ``variants__attributes__<key>``. For relational-source facets the
    lookup is the ``field_name`` directly.

    Args:
        query_params (dict): the request query parameters.

    Returns:
        dict: validated ``{lookup: value}`` pairs ready for queryset
            filtering.
    """
    facets = get_active_facets()
    facet_lookups = {}
    for facet in facets:
        if facet.source_field == "product_specs" and facet.key:
            facet_lookups[facet.key] = f"specs__{facet.key}"
        elif facet.source_field == "variant_attributes" and facet.key:
            facet_lookups[facet.key] = f"variants__attributes__{facet.key}"
        elif facet.source_field == "product_field" and facet.field_name:
            facet_lookups[facet.field_name] = facet.field_name
        elif facet.source_field == "variant_field" and facet.field_name:
            facet_lookups[facet.field_name] = f"variants__{facet.field_name}"

    applied_filters = {}
    for param_key, lookup in facet_lookups.items():
        value = query_params.get(param_key)
        if value is not None and value != "":
            applied_filters[lookup] = value

    return applied_filters
