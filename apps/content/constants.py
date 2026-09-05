"""Tunable constants for the content app.

Field-length ceilings and placement validation live here so they are
reviewable in one place rather than scattered across models, serializers,
and services.
"""

PAGE_TITLE_MAX_LENGTH = 255
PAGE_BODY_MAX_LENGTH = 50000
BANNER_TITLE_MAX_LENGTH = 255
BANNER_LINK_URL_MAX_LENGTH = 500
BANNER_PLACEMENT_MAX_LENGTH = 50
