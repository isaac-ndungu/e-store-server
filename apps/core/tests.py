"""Tests for the core app.

Covers the public site-config endpoints, the singleton invariant enforced by
the model, the admin guards, and the shared ``public`` throttle scope.
"""

from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.admin import SiteConfigAdmin
from apps.core.models import SiteConfig

SITE_CONFIG_URL = reverse("api:core:site-config")
LEGAL_URL = reverse("api:core:site-config-legal")

# The 'public' scope is configured as 100/min in settings.py; keep in sync.
PUBLIC_RATE_LIMIT = 100


class SiteConfigEndpointTests(APITestCase):
    """Exercises the two public read-only site-config endpoints."""

    def setUp(self):
        cache.clear()

    def test_site_config_is_public_and_returns_default_row(self):
        """An anonymous caller gets a 200 and the default singleton is created."""
        response = self.client.get(SITE_CONFIG_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, dict)
        self.assertEqual(response.data["site_name"], "My Store")
        self.assertEqual(str(response.data["standard_vat_rate"]), "16.00")
        self.assertEqual(SiteConfig.objects.count(), 1)

    def test_site_config_exposes_storefront_fields(self):
        """The full config payload carries theme, settings, and legal atoms."""
        response = self.client.get(SITE_CONFIG_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in ("theme", "settings", "kra_pin", "return_policy_text", "domain"):
            self.assertIn(key, response.data)

    def test_legal_endpoint_returns_legal_fields_only(self):
        """The legal endpoint returns trust/legal copy and not storefront JSON."""
        response = self.client.get(LEGAL_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in (
            "site_name",
            "kra_pin",
            "business_registration_number",
            "physical_address",
            "return_policy_text",
            "cooling_off_period_days",
            "support_phone",
        ):
            self.assertIn(key, response.data)
        for secret in ("theme", "settings"):
            self.assertNotIn(secret, response.data)

    def test_public_scope_throttles_after_rate_limit(self):
        """Bursting past the public rate limit yields HTTP 429."""
        for _ in range(PUBLIC_RATE_LIMIT):
            response = self.client.get(SITE_CONFIG_URL)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(SITE_CONFIG_URL)
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class SiteConfigSingletonTests(APITestCase):
    """Verifies the ``pk=1`` singleton invariant at the model layer."""

    def test_load_creates_single_default_row(self):
        """Calling load() twice never leaves more than one row."""
        first = SiteConfig.load()
        second = SiteConfig.load()
        self.assertEqual(first.pk, 1)
        self.assertEqual(second.pk, 1)
        self.assertEqual(SiteConfig.objects.count(), 1)

    def test_fresh_row_has_seeded_settings_defaults(self):
        """A new singleton carries the seeded settings defaults, not an empty dict."""
        config = SiteConfig.load()
        settings = config.settings
        self.assertEqual(settings["currency"], "KES")
        self.assertEqual(settings["default_payment_method"], "mpesa")
        self.assertEqual(settings["otp_expiry_minutes"], 10)
        self.assertEqual(settings["stock_reservation_grace_minutes"], 15)
        self.assertEqual(settings["volumetric_weight_divisor"], 5000)
        self.assertIs(settings["shipping_is_vatable"], True)

    def test_save_forces_pk_one(self):
        """Any save lands on row 1 instead of creating a second record."""
        SiteConfig.load()
        SiteConfig.objects.create(site_name="Second Store")
        self.assertEqual(SiteConfig.objects.count(), 1)
        self.assertEqual(SiteConfig.objects.get().site_name, "Second Store")
        self.assertEqual(SiteConfig.objects.get().pk, 1)

    def test_admin_cannot_add_or_delete_singleton(self):
        """The admin page forbids creating a second row or deleting the one."""
        admin_instance = SiteConfigAdmin(SiteConfig, AdminSite())
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(
            admin_instance.has_delete_permission(request=None, obj=SiteConfig.load())
        )
