"""Data models for the accounts app.

Holds the ``User`` model, used only for staff/admin authentication, and the
shared delivery ``Address`` directory staff reuse across orders.
Authentication identity is email + password; ``phone_number`` is the contact
record kept per staff account.
"""

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class User(AbstractUser):
    """Extends Django's built-in user with email login and a contact phone.

    ``email`` is unique and is the login credential via ``USERNAME_FIELD``.
    ``username`` and ``password`` are inherited as-is; ``username`` remains
    required (``REQUIRED_FIELDS``) for display/uniqueness. ``phone_number``
    is the contact number kept on the staff record.
    """

    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=15)
    # Set to True only server-side after a phone number has been verified
    # (e.g. via an OTP flow). No such flow exists yet, so it stays False;
    # it is never set by a client and is never auto-set at registration.
    phone_verified = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "phone_number"]

    class Meta:
        indexes = [
            models.Index(Lower("email"), name="user_email_lower_idx"),
        ]

    def __str__(self):
        """Return the login credential (email) for admin/trace output."""
        return self.email


class Address(models.Model):
    """A reusable delivery address in the staff directory.

    There are no customer accounts, so addresses are not owned by shoppers:
    staff keep repeat-delivery addresses (regulars, repeat business buyers)
    here to avoid retyping them on every intake, and reference one from an
    order via ``shipping_address_id``. ``user`` is null for directory entries;
    surviving rows from retired customer accounts keep their link for history.
    ``is_default`` marks the entry staff pre-fill intake with.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="addresses",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
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
            models.Index(fields=["is_default"], name="addr_default_idx"),
            models.Index(fields=["created_at"], name="addr_created_idx"),
        ]

    def __str__(self):
        """Return a short human-readable label for admin/trace output."""
        return f"{self.recipient_name}  -  {self.area_name}, {self.county}"
