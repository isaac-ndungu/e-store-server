"""Selectors for the accounts app.

Query helpers that keep views thin and give a single reference for how a
logged-in user's addresses are fetched.
"""

from apps.accounts.models import Address


def get_addresses_for_user(user):
    """Return the address queryset belonging to a user, newest first.

    Args:
        user (User): the account whose addresses are wanted.

    Returns:
        QuerySet: the user's ``Address`` rows ordered by most recently created.
    """
    if not user.is_authenticated:
        return Address.objects.none()
    return user.addresses.all()
