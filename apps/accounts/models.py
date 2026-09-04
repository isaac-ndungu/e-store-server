"""Data models for the accounts app.

Holds the custom ``User`` and the delivery ``Address``. Authentication identity
is email + password; ``phone_number`` is captured at registration and used for
order contact, M-Pesa, OTP delivery, and guest/loyalty identification, but never
to authenticate a login.
"""

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Extends Django's built-in user with email login and a contact phone.

    ``email`` is unique and is the login credential via ``USERNAME_FIELD``.
    ``username`` and ``password`` are inherited as-is; ``username`` remains
    required at registration (``REQUIRED_FIELDS``) for display/uniqueness.
    ``phone_number`` is the platform-wide contact identifier — order contact,
    M-Pesa transactions, OTP delivery, and guest/loyalty lookup — but is never
    used to authenticate a login.
    """

    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=15)
    # Set to True only server-side after a phone number has been verified
    # (e.g. via an OTP flow). No such flow exists yet, so it stays False;
    # it is never set by a client and is never auto-set at registration.
    phone_verified = models.BooleanField(default=False)

    ROLE_CHOICES = (
        ("customer", "Customer"),
        ("manager", "Manager"),
        ("support", "Support"),
        ("analyst", "Analyst"),
        ("courier", "Courier"),
    )
    # Role drives business-rule permissions (which endpoints a staff member may
    # reach and what data they can act on). It is independent of ``is_staff``,
    # which remains the coarse gate for Django's admin interface.
    role = models.CharField(
        max_length=12, choices=ROLE_CHOICES, default="customer", db_index=True
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "phone_number"]

    def has_role(self, *roles):
        """Return whether the user holds any of the given roles.

        A superuser is considered to hold every role, so an operator with
        ``is_superuser`` is never locked out of a role-gated action.

        Args:
            *roles (str): role keys to test against.

        Returns:
            bool: True when the user's role is among ``roles`` or the user is
                a superuser.
        """
        return self.is_superuser or self.role in roles

    def __str__(self):
        """Return the login credential (email) for admin/trace output."""
        return self.email


class Address(models.Model):
    """A delivery address belonging to a user.

    ``phone_number`` is the delivery-contact number and may differ from the
    account's own ``phone_number`` (ordering delivery to a different contact).
    ``is_default`` marks the preferred address; exactly one is not enforced at
    the database level, the owning code keeps it consistent.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="addresses",
        on_delete=models.CASCADE,
    )
    label = models.CharField(max_length=50, blank=True)
    recipient_name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=15)
    county = models.CharField(max_length=100)
    area_name = models.CharField(max_length=255)
    landmark_description = models.TextField(blank=True)
    building_or_estate = models.CharField(max_length=255, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_default"], name="addr_user_default_idx"),
            models.Index(fields=["county"], name="addr_county_idx"),
        ]

    def __str__(self):
        """Return a short human-readable label for admin/trace output."""
        return f"{self.recipient_name} — {self.area_name}, {self.county}"
