"""Product search: synonym expansion, typo tolerance, and relevance ranking.

PostgreSQL stores GIN trigram indexes over the product name, SKU, and short
and long descriptions. The indexable pg_trgm form is the ``%`` operator, which
Django exposes as the ``trigram_similar`` lookup. A ``similarity(...) > x``
predicate cannot use a GIN trigram index, so filtering uses ``%`` and ranking
uses ``TrigramSimilarity`` only after the candidate rows have been narrowed.

Brand and category names live in small related tables with no trigram index.
They are resolved to ids first and folded into the product filter as
``brand_id__in`` / ``category_id__in``, which keeps every branch of the filter
index-backed.

The synonym groups below are the single place search vocabulary is expanded.
Each group is a set of interchangeable shopper terms; a query containing one
member is also tried with the others so "fridge" finds a "Refrigerator".
"""

import re
import unicodedata

from django.db import connection
from django.db.models import Case, FloatField, IntegerField, Q, Value, When
from django.db.models.functions import Greatest

from apps.catalog.models import Brand, Category

# Interchangeable shopper terms. Keep every group a flat tuple of lowercase
# phrases; phrase entries are matched as whole words in the normalized query.
SYNONYM_GROUPS = (
    ("fridge", "refrigerator", "freezer"),
    ("tv", "television"),
    ("cooker", "stove"),
    ("washer", "washing machine"),
    ("stabilizer", "stabiliser", "voltage regulator"),
    ("ac", "air conditioner"),
    ("instant shower", "water heater"),
    ("extension", "socket", "surge protector"),
)

# Queries shorter than this cannot produce a useful trigram, so they fall back
# to substring matching instead of the ``%`` operator.
MIN_TRIGRAM_LENGTH = 3

_MAX_TERM_LENGTH = 200
_WORD_SEPARATOR = re.compile(r"[^a-z0-9]+")


def normalize_search_term(raw):
    """Return a lowercase, punctuation-free, single-spaced search term.

    Args:
        raw (str | None): the raw ``search`` or ``q`` query parameter.

    Returns:
        str: the normalized term, truncated to a safe length.
    """
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(raw))
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    words = _WORD_SEPARATOR.sub(" ", lowered).split()
    return " ".join(words)[:_MAX_TERM_LENGTH]


def expand_search_terms(raw):
    """Expand a query with every synonym phrase it contains.

    Starts from the normalized query and, for each synonym group whose member
    appears as a whole word/phrase, adds a variant with that member replaced by
    each alternative in the group. Results are sorted and de-duplicated so
    filter construction is deterministic.

    Args:
        raw (str | None): the raw search term.

    Returns:
        list[str]: the original term plus its synonym variants.
    """
    normalized = normalize_search_term(raw)
    if normalized == "":
        return []
    variants = {normalized}
    # SKUs are commonly typed or stored with hyphens ("FRG-300"), while the
    # normalizer collapses separators to spaces for phrase matching. Keep a
    # hyphen-joined variant so either form finds the SKU.
    if " " in normalized:
        variants.add("-".join(normalized.split()))
    for group in SYNONYM_GROUPS:
        for member in group:
            pattern = re.compile(rf"(?<![a-z0-9]){re.escape(member)}(?![a-z0-9])")
            if not pattern.search(normalized):
                continue
            for alternative in group:
                variants.add(pattern.sub(alternative, normalized))
    return sorted(variants)


def _matching_related_ids(model, terms, active_only=False):
    """Return ids of related rows whose name matches any expanded term.

    Args:
        model (type): ``Brand`` or ``Category``.
        terms (list[str]): normalized query variants.
        active_only (bool): when True, only active rows are considered.

    Returns:
        list[int]: matching primary keys.
    """
    query = Q()
    for term in terms:
        query |= Q(name__icontains=term)
    rows = model.objects.filter(query)
    if active_only:
        rows = rows.filter(is_active=True)
    return list(rows.values_list("pk", flat=True))


def _postgres_search_filter(terms):
    """Build an index-friendly trigram filter for PostgreSQL.

    Args:
        terms (list[str]): expanded query variants.

    Returns:
        Q: the combined filter.
    """
    query = Q()
    # Hyphenated input is almost always a SKU fragment such as "FRG-300".
    # Trigram similarity cannot tell FRG-300 from FRG-200, and the expanded
    # "frg 300" variant would still match the wrong SKU, so every SKU branch
    # becomes a substring match for the whole query.
    sku_is_precise = any("-" in term for term in terms)
    for term in terms:
        if len(term) >= MIN_TRIGRAM_LENGTH:
            query |= Q(name__trigram_similar=term)
            if sku_is_precise:
                query |= Q(sku__icontains=term)
            else:
                query |= Q(sku__trigram_similar=term)
            query |= Q(short_description__trigram_similar=term)
            query |= Q(description__trigram_similar=term)
        else:
            query |= Q(name__icontains=term)
            query |= Q(sku__icontains=term)

    brand_ids = _matching_related_ids(Brand, terms)
    category_ids = _matching_related_ids(Category, terms, active_only=True)
    if brand_ids:
        query |= Q(brand_id__in=brand_ids)
    if category_ids:
        query |= Q(category_id__in=category_ids)
    return query


def _fallback_search_filter(terms):
    """Build a portable substring filter for non-PostgreSQL backends.

    Args:
        terms (list[str]): expanded query variants.

    Returns:
        Q: the combined filter.
    """
    query = Q()
    for term in terms:
        query |= Q(name__icontains=term)
        query |= Q(sku__icontains=term)
        query |= Q(short_description__icontains=term)
        query |= Q(description__icontains=term)
        query |= Q(brand__name__icontains=term)
        query |= Q(category__name__icontains=term)
    return query


def filter_products_by_search(queryset, raw):
    """Filter products by a typo-tolerant, synonym-aware search term.

    Empty or whitespace-only queries leave the queryset untouched.

    Args:
        queryset (QuerySet): the product queryset to filter.
        raw (str | None): the raw search term.

    Returns:
        QuerySet: products matching at least one expanded term.
    """
    terms = expand_search_terms(raw)
    if not terms:
        return queryset
    if connection.vendor == "postgresql":
        return queryset.filter(_postgres_search_filter(terms))
    return queryset.filter(_fallback_search_filter(terms))


def order_products_by_search(queryset, raw):
    """Order products by exact name, then prefix, then trigram similarity.

    The tiers are: an exact case-insensitive name match to any expanded term,
    then a name that starts with any expanded term, then everything else
    ordered by the best trigram similarity across name, brand, category, and
    SKU. Non-PostgreSQL backends keep their existing order.

    Args:
        queryset (QuerySet): the already-filtered product queryset.
        raw (str | None): the raw search term.

    Returns:
        QuerySet: the relevance-ordered queryset.
    """
    terms = expand_search_terms(raw)
    if not terms or connection.vendor != "postgresql":
        return queryset

    from django.contrib.postgres.search import TrigramSimilarity

    exact_matches = [When(name__iexact=term, then=Value(0)) for term in terms]
    prefix_matches = [When(name__istartswith=term, then=Value(1)) for term in terms]
    tier = Case(
        *exact_matches,
        *prefix_matches,
        default=Value(2),
        output_field=IntegerField(),
    )

    similarities = []
    for term in terms:
        similarities.extend(
            (
                TrigramSimilarity("name", term),
                TrigramSimilarity("brand__name", term),
                TrigramSimilarity("category__name", term),
                TrigramSimilarity("sku", term),
            )
        )
    best_similarity = Greatest(*similarities, output_field=FloatField())

    return queryset.annotate(
        search_tier=tier,
        search_similarity=best_similarity,
    ).order_by("search_tier", "-search_similarity", "-created_at")
