"""Shared meal domain constants.

Canonical source for meal labels. Other modules should import from here
instead of re-implementing meal logic.
"""

# Canonical meal type labels
MEAL_LABELS: dict[str, str] = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "snack": "零食",
}

VALID_MEAL_TYPES: set[str] = set(MEAL_LABELS)
