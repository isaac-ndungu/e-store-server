"""Kenyan geography constants for shipping.

Delivery zones are priced at the county + delivery-area granularity that
Kenyan couriers use, so the zone's ``county`` field is validated against the
47 official counties rather than accepting arbitrary free text. Consistent
county values keep the storefront filter, reporting, and any future courier
integration working from clean data. This module is the single place to edit
when the platform is reskinned for another country.
"""

KENYAN_COUNTIES = (
    "Baringo",
    "Bomet",
    "Bungoma",
    "Busia",
    "Elgeyo-Marakwet",
    "Embu",
    "Garissa",
    "Homa Bay",
    "Isiolo",
    "Kajiado",
    "Kakamega",
    "Kericho",
    "Kiambu",
    "Kilifi",
    "Kirinyaga",
    "Kisii",
    "Kisumu",
    "Kitui",
    "Kwale",
    "Laikipia",
    "Lamu",
    "Machakos",
    "Makueni",
    "Mandera",
    "Marsabit",
    "Meru",
    "Migori",
    "Mombasa",
    "Murang'a",
    "Nairobi",
    "Nakuru",
    "Nandi",
    "Narok",
    "Nyamira",
    "Nyandarua",
    "Nyeri",
    "Samburu",
    "Siaya",
    "Taita-Taveta",
    "Tana River",
    "Tharaka-Nithi",
    "Trans-Nzoia",
    "Turkana",
    "Uasin Gishu",
    "Vihiga",
    "Wajir",
    "West Pokot",
)

_COUNTY_ALIASES = {
    "nairobi city": "nairobi",
}

_PUNCTUATION = ("/", "-", "_", ".")


def _normalize(county):
    """Return a punctuation-free, lowercased county name for comparison.

    Args:
        county (str): the county name to normalize.

    Returns:
        str: the name lowercased, stripped, and with apostrophes, hyphens,
            slashes, and underscores folded to nothing or spaces.
    """
    compact = county.lower().strip().replace("'", "").replace("\u2019", "")
    for marker in _PUNCTUATION:
        compact = compact.replace(marker, " ")
    return " ".join(compact.split())


_COUNTY_LOOKUP = frozenset(_normalize(county) for county in KENYAN_COUNTIES)


def is_valid_county(county):
    """Return True when ``county`` names one of the 47 Kenyan counties.

    Matching is tolerant of apostrophes, hyphens, slashes, and Nairobi's
    ``City`` suffix, so a slightly different spelling than the canonical list
    still validates.

    Args:
        county (str): the county name to check.

    Returns:
        bool: whether the name resolves to a known county.
    """
    normalized = _normalize(county)
    if normalized in _COUNTY_LOOKUP:
        return True
    return _COUNTY_ALIASES.get(normalized) is not None
