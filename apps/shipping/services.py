"""Services for the shipping app.

Write-path helpers for the delivery-area list live here so views and
management commands share one normalization and upsert path. Reads stay in
``selectors``.
"""

import re

from apps.shipping.constants import canonical_county
from apps.shipping.models import DeliveryArea

_WHITESPACE = re.compile(r"\s+")


def normalize_area_name(raw_name):
    """Return a storefront-ready area label for a courier town name.

    Courier feeds use all-caps names with uneven spacing (``"  NANYUKI
    TOWN"``, ``"MAKONGENI,THIKA"``). The picker reads better in title case
    with single spaces and a space after each comma.

    Args:
        raw_name (str): the town name from the feed or staff input.

    Returns:
        str: the cleaned display name, or an empty string when blank.
    """
    cleaned = _WHITESPACE.sub(" ", (raw_name or "").strip())
    cleaned = cleaned.replace(" ,", ",").replace(",", ", ")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned.title() if cleaned else ""


def build_delivery_area_pair(county, town):
    """Resolve a courier county + town pair to canonical ``DeliveryArea`` values.

    Args:
        county (str): the raw county name from the courier feed.
        town (str): the raw town name from the courier feed.

    Returns:
        tuple[str, str] | None: ``(county, area_name)`` with the official
            county spelling and a normalized area label, or None when either
            side is blank or the county is not a known Kenyan county.
    """
    canonical = canonical_county(county or "")
    area_name = normalize_area_name(town or "")
    if canonical is None or not area_name:
        return None
    return canonical, area_name


def sync_delivery_areas(pairs, reactivate=True):
    """Upsert delivery areas for county + town pairs.

    The operation is idempotent: existing rows are kept (and reactivated when
    requested) rather than duplicated, so the sync is safe to run twice.

    Args:
        pairs (Iterable[tuple[str, str]]): raw ``(county, town)`` pairs.
        reactivate (bool): when True, set ``is_active=True`` on rows that
            already exist but were deactivated.

    Returns:
        dict: counts shaped as ``{"created": int, "reactivated": int,
            "skipped": int}`` where ``skipped`` covers blank or unknown-county
            rows.
    """
    created = 0
    reactivated = 0
    skipped = 0
    seen = set()

    for county, town in pairs:
        resolved = build_delivery_area_pair(county, town)
        if resolved is None:
            skipped += 1
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        canonical, area_name = resolved
        area, was_created = DeliveryArea.objects.get_or_create(
            county=canonical,
            area_name=area_name,
            defaults={"is_active": True},
        )
        if was_created:
            created += 1
        elif reactivate and not area.is_active:
            area.is_active = True
            area.save(update_fields=["is_active"])
            reactivated += 1

    return {"created": created, "reactivated": reactivated, "skipped": skipped}
