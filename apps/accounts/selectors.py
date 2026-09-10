"""Selectors for the accounts app.

Query helpers that keep views thin and give a single reference for how the
shared staff address directory is fetched.
"""

from apps.accounts.models import Address


def list_addresses():
    """Return every directory address, newest first.

    Returns:
        QuerySet: all ``Address`` rows with the linked staff account (if any)
            pre-fetched, ordered by most recently created.
    """
    return Address.objects.select_related("user").order_by("-created_at", "-pk")
