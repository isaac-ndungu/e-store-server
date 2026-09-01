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
    phone_verified = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "phone_number"]

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

    def __str__(self):
        """Return a short human-readable label for admin/trace output."""
        return f"{self.recipient_name} — {self.area_name}, {self.county}"
