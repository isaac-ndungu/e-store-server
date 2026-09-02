"""Domain constants for the collections app.

Keeps the set of smart rules in one place so the model, serializers, and the
refresh dispatch all agree on which rules exist.
"""

SMART_RULE_CHOICES_VALUES = (
    "new_arrivals",
    "restocked",
    "on_sale",
    "best_sellers",
    "low_stock",
)
