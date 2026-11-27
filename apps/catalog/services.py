"""Business logic for the catalog app.

CRUD operations, slug generation, facet aggregation, and query-param
validation for the faceted product search. Views stay thin  -  all write
logic and non-trivial read computation lives here.

Facet aggregation runs one aggregate query per active ``FacetDefinition``
per distinct filter combination on a cold cache, then serves repeats from
Redis under the current facet generation (bumped on facet or product
changes).
"""

import hashlib
import re
import unicodedata
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog import cache as catalog_cache
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

# Maximum number of active facets allowed to bound query cost.
MAX_ACTIVE_FACETS = 15


# Slug generation


def generate_unique_slug(instance, value, slug_field="slug"):
    """Generate a URL-safe unique slug from ``value``.

    Strips accents, lowercases, replaces non-alphanumeric characters with
    hyphens, truncates to the model field's maximum length, and appends a
    numeric suffix if the slug already exists.

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

    max_length = instance.__class__._meta.get_field(slug_field).max_length
    if max_length:
        slug = slug[:max_length].rstrip("-")

    base_slug = slug
    counter = 1
    queryset = instance.__class__._default_manager.all()
    if instance.pk:
        queryset = queryset.exclude(pk=instance.pk)

    while queryset.filter(**{slug_field: slug}).exists():
        suffix = f"-{counter}"
        if max_length:
            prefix = base_slug[: max(max_length - len(suffix), 0)].rstrip("-")
            slug = f"{prefix}{suffix}"
        else:
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
    price,
    images=None,
    related_products=None,
    pricing_tiers=None,
    **kwargs,
):
    """Create a product with its nested children atomically.

    Wraps the full creation in ``transaction.atomic()`` so that a failure
    partway through (e.g. a duplicate SKU) rolls back the product and any
    already-created children, preventing orphaned rows.

    Args:
        name (str): the product name.
        slug (str | None): explicit slug, or None to auto-generate.
        sku (str): the product SKU (must be unique).
        price (Decimal): the sellable unit price.
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
        product = Product.objects.create(
            name=name, slug=slug, sku=sku, price=price, **kwargs
        )

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
                PricingTier.objects.create(product=product, **tier_data)

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


# Product FAQ lifecycle

FAQ_MAX_ITEMS = 20
FAQ_QUESTION_MIN_LENGTH = 3
FAQ_ANSWER_MIN_LENGTH = 10


def normalize_faq_question(value):
    """Return a single-line FAQ question with control characters removed.

    Args:
        value (str): the raw question text.

    Returns:
        str: the trimmed, whitespace-collapsed question.
    """
    text = "".join(
        char for char in str(value or "") if unicodedata.category(char)[0] != "C"
    )
    return re.sub(r"\s+", " ", text).strip()


def normalize_faq_answer(value):
    """Return FAQ answer text with control characters and excess blank lines removed.

    Line breaks are preserved so staff can separate short paragraphs.

    Args:
        value (str): the raw answer text.

    Returns:
        str: the cleaned answer.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        char
        for char in text
        if char in {"\n", "\t"} or unicodedata.category(char)[0] != "C"
    )
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def validate_product_faq_rows(rows, *, product=None):
    """Validate and normalize an ordered list of FAQ rows.

    Both-empty rows are ignored so the staff editor can leave a blank row
    in place. Every other row must contain both fields. Questions are unique
    per product ignoring case, and HTML angle brackets are rejected because
    the storefront renders answers as plain text.

    Args:
        rows (list[dict]): raw rows with ``question``, ``answer``, and an
            optional ``id`` for an existing row.
        product (Product | None): parent product used to verify submitted
            row IDs belong to it. When omitted, ownership is left to the
            caller.

    Returns:
        tuple[list[dict], dict]: normalized rows and row-indexed errors.
    """
    errors = {}
    cleaned_rows = []
    seen_questions = {}
    seen_ids = {}
    id_rows = []

    for index, row in enumerate(rows):
        question = normalize_faq_question(row.get("question"))
        answer = normalize_faq_answer(row.get("answer"))
        row_id = row.get("id")

        if not question and not answer:
            continue

        row_errors = {}
        if row_id is not None:
            if row_id in seen_ids:
                row_errors["non_field_errors"] = (
                    "This FAQ row is listed more than once."
                )
            else:
                seen_ids[row_id] = index
                id_rows.append((index, row_id))

        if not question:
            row_errors["question"] = "Enter a question or remove this row."
        else:
            if len(question) < FAQ_QUESTION_MIN_LENGTH:
                row_errors["question"] = (
                    f"Question must be at least {FAQ_QUESTION_MIN_LENGTH} characters."
                )
            elif "<" in question or ">" in question:
                row_errors["question"] = "Use plain text only; HTML is not allowed."

        if not answer:
            row_errors["answer"] = "Enter an answer or remove this row."
        else:
            if len(answer) < FAQ_ANSWER_MIN_LENGTH:
                row_errors["answer"] = (
                    f"Answer must be at least {FAQ_ANSWER_MIN_LENGTH} characters."
                )
            elif "<" in answer or ">" in answer:
                row_errors["answer"] = "Use plain text only; HTML is not allowed."

        if question and answer:
            normalized_question = question.casefold()
            if normalized_question in seen_questions:
                row_errors["question"] = (
                    "This question is already used on this product."
                )
            else:
                seen_questions[normalized_question] = index

            if len(cleaned_rows) >= FAQ_MAX_ITEMS:
                row_errors["non_field_errors"] = (
                    f"A product can have at most {FAQ_MAX_ITEMS} questions."
                )

        if row_errors:
            errors[str(index)] = row_errors
            continue

        cleaned_row = {
            "question": question,
            "answer": answer,
            "position": len(cleaned_rows),
        }
        if row_id is not None:
            cleaned_row["id"] = row_id
        cleaned_rows.append(cleaned_row)

    if product is not None and id_rows:
        existing_ids = set(
            ProductFAQ.objects.filter(
                product=product, pk__in=[row_id for _, row_id in id_rows]
            ).values_list("pk", flat=True)
        )
        for index, row_id in id_rows:
            if row_id not in existing_ids:
                row_errors = errors.setdefault(str(index), {})
                row_errors["non_field_errors"] = (
                    "This FAQ row no longer exists on this product. "
                    "Refresh and try again."
                )

    return cleaned_rows, errors


def replace_product_faqs(product, rows):
    """Replace every FAQ on a product with the supplied ordered rows.

    Existing rows are updated in place when a submitted row carries its
    ``id``, so public FAQ anchors stay stable across content edits and
    reorders. Rows omitted from ``rows`` are deleted, and rows without an id
    are created. The whole write runs in one transaction so a failure cannot
    leave a product with a partially replaced FAQ list.

    Args:
        product (Product): the product whose FAQs are replaced.
        rows (list[dict]): validated rows with question, answer, position,
            and an optional id.

    Returns:
        list[ProductFAQ]: the saved ordered FAQ rows.
    """
    from apps.catalog.tasks import schedule_storefront_product_revalidation

    with transaction.atomic():
        existing = {faq.pk: faq for faq in product.faqs.select_for_update().all()}
        retained_ids = set()
        for row in rows:
            row_id = row.get("id")
            if row_id in existing and row_id not in retained_ids:
                retained_ids.add(row_id)

        if retained_ids:
            product.faqs.exclude(pk__in=retained_ids).delete()
        else:
            product.faqs.all().delete()

        existing_updates = []
        used_ids = set()
        for row in rows:
            row_id = row.get("id")
            if row_id in retained_ids and row_id not in used_ids:
                used_ids.add(row_id)
                existing_updates.append((existing[row_id], row))

        if existing_updates:
            now = timezone.now()
            max_position = max((faq.position for faq in existing.values()), default=0)
            temp_base = max_position + 1
            if temp_base + len(existing_updates) - 1 > 32767:
                # No room above existing positions for a conflict-free swap.
                ProductFAQ.objects.filter(pk__in=retained_ids).delete()
                ProductFAQ.objects.bulk_create(
                    [
                        ProductFAQ(
                            id=faq.pk,
                            product=product,
                            question=row["question"],
                            answer=row["answer"],
                            position=row["position"],
                        )
                        for faq, row in existing_updates
                    ]
                )
            else:
                for offset, (faq, _row) in enumerate(existing_updates):
                    faq.position = temp_base + offset
                    faq.question = f"<faq-tmp-{faq.pk}-{uuid4().hex}>"
                    faq.updated_at = now
                ProductFAQ.objects.bulk_update(
                    [faq for faq, _row in existing_updates],
                    ["position", "question", "updated_at"],
                )

                for faq, row in existing_updates:
                    faq.question = row["question"]
                    faq.answer = row["answer"]
                    faq.position = row["position"]
                    faq.updated_at = now
                ProductFAQ.objects.bulk_update(
                    [faq for faq, _row in existing_updates],
                    ["question", "answer", "position", "updated_at"],
                )

        new_rows = [
            ProductFAQ(
                product=product,
                question=row["question"],
                answer=row["answer"],
                position=row["position"],
            )
            for row in rows
            if row.get("id") not in used_ids
        ]
        if new_rows:
            ProductFAQ.objects.bulk_create(new_rows)

    catalog_cache.bump_product_list_generation()
    schedule_storefront_product_revalidation(product.pk)
    return list(product.faqs.all())


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

    Results are cached per filter combination (hashed from the filtered
    queryset SQL) under the current facet generation, so repeated
    storefront listing requests with identical filters skip up to
    ``MAX_ACTIVE_FACETS`` aggregate queries. Any facet definition or
    product change bumps the generation and invalidates the entries.

    Args:
        queryset (QuerySet): the base product queryset (already filtered by
            any non-facet filters like category/brand).

    Returns:
        dict: ``{"facet_name": {value/count or min/max}, ...}``
    """
    generation = catalog_cache.get_facet_generation()
    try:
        query_hash = hashlib.sha1(str(queryset.query).encode()).hexdigest()
    except Exception:
        query_hash = ""
    if query_hash:
        cached = catalog_cache.get_cached_facet_counts(generation, query_hash)
        if cached is not None:
            return cached
    facets = get_active_facets()
    result = {}

    for facet in facets:
        if facet.facet_type == "range":
            result[facet.name] = _range_stats_for_facet(queryset, facet)
        else:
            result[facet.name] = _choice_counts_for_facet(queryset, facet)

    if query_hash:
        catalog_cache.cache_facet_counts(generation, query_hash, result)
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

    elif facet.source_field == "product_field" and facet.field_name:
        if facet.field_name in FacetDefinition.FACETABLE_PRODUCT_FIELDS:
            rows = (
                queryset.exclude(**{facet.field_name: None})
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
    elif facet.source_field == "product_field" and facet.field_name:
        if facet.field_name in FacetDefinition.FACETABLE_PRODUCT_FIELDS:
            values = (
                queryset.exclude(**{facet.field_name: None})
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
    type sensitive  -  an integer ``200`` stored in ``specs`` is not equal to
    the string ``"200"``  -  numeric-looking values also match their typed
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
        elif facet.source_field == "product_field" and facet.field_name:
            param_key, lookup = facet.field_name, facet.field_name
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
