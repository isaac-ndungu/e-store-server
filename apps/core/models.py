"""Data model for the core app.

Holds the single runtime business-settings record (``SiteConfig``), the
data-level counterpart to ``config/settings.py``. ``config/`` is deployed
code configuration; ``SiteConfig`` is staff-editable business data (VAT rate,
KRA PIN, legal copy, feature toggles) that changes without a deploy.
"""

from django.db import models


def default_site_settings():
    """Return the recommended default ``settings`` JSON for a fresh install.

    Seeds every feature toggle and business constant downstream (OTP expiry,
    stock-reservation grace, loyalty rates, shipping VAT) with a sensible
    starting value out of the box. Because staff can edit ``settings`` at
    runtime, readers must still use ``.get(key, default)`` rather than direct
    indexing.

    Returns:
        dict: the default ``settings`` payload.
    """
    return {
        "product_type": "appliance",
        "currency": "KES",
        "enable_bundles": True,
        "enable_loyalty": True,
        "enable_social_proof": True,
        "enable_b2b_quotes": True,
        "enable_wishlist": True,
        "enable_reviews": True,
        "payment_methods": ["mpesa", "cod", "card", "invoice"],
        "default_payment_method": "mpesa",
        "smart_collection_refresh_minutes": 15,
        "otp_expiry_minutes": 10,
        "stock_reservation_grace_minutes": 15,
        "loyalty_points_earned_per_kes_spent": 1,
        "loyalty_points_per_kes_redeemed": 10,
        "volumetric_weight_divisor": 5000,
        "shipping_is_vatable": True,
    }


class SiteConfig(models.Model):
    """Runtime-editable business settings singleton, distinct from ``settings.py``.

    Exactly one row exists, pinned to ``pk=1`` by ``save()`` and addressed via
    ``load()``. Because the VAT rate and KRA PIN live here, the singleton must
    never be deleted or duplicated — the ``pk=1`` pin and the admin guards below
    enforce that.
    """

    site_name = models.CharField(max_length=255, default="My Store")
    tagline = models.CharField(max_length=255, blank=True)
    domain = models.CharField(max_length=255, blank=True)
    logo = models.ImageField(upload_to="site/logo/", blank=True)
    favicon = models.ImageField(upload_to="site/favicon/", blank=True)
    theme = models.JSONField(default=dict)
    settings = models.JSONField(default=default_site_settings)
    contact_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=20, blank=True)
    social_links = models.JSONField(default=dict, blank=True)
    kra_pin = models.CharField(max_length=20, blank=True)
    business_registration_number = models.CharField(max_length=50, blank=True)
    physical_address = models.TextField(blank=True)
    return_policy_text = models.TextField(blank=True)
    cooling_off_period_days = models.PositiveIntegerField(default=7)
    standard_vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=16.00
    )

    def save(self, *args, **kwargs):
        """Force the singleton identity: every write lands on the same row.

        ``force_insert`` must be dropped: ``objects.create()`` and
        ``get_or_create()`` pass it, which would make Django INSERT a duplicate
        row with ``pk=1`` instead of updating the existing one.
        """
        self.pk = 1
        kwargs.pop("force_insert", None)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """Return the singleton row, creating the default record on first use.

        Returns:
            SiteConfig: the one-and-only configuration row.
        """
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        """Return a human-readable label for admin/trace output."""
        return self.site_name
