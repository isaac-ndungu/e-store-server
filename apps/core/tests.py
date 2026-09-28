"""Tests for the core app.

Covers the public site-config endpoints, the singleton invariant enforced by
the model, the admin guards, the shared ``public`` throttle scope, the
load-test seed command's idempotent-insert / full-cleanup contracts, and the
Cloudinary media storage's per-file resource-type routing.
"""

from io import StringIO
from unittest import mock

import cloudinary
from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.catalog.models import Product
from apps.core.admin import SiteConfigAdmin
from apps.core.models import SiteConfig
from apps.core.storage import CloudinaryMediaStorage
from apps.orders.models import Order, OrderItem
from apps.social_proof.models import ProductViewEvent

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
        self.assertEqual(settings["payment_methods"], ["mpesa", "cod", "bank_transfer"])
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


class SeedLoadTestDataTests(APITestCase):
    """Exercises the load-test seed command's insert and cleanup contracts."""

    def _run(self, **kwargs):
        """Run the seed command with the given options.

        Args:
            kwargs: options forwarded to the command.

        Returns:
            str: the command stdout.
        """
        out = StringIO()
        call_command(
            "seed_load_test_data",
            stdout=out,
            products=20,
            orders=50,
            users=10,
            views=40,
            **kwargs,
        )
        return out.getvalue()

    def test_seed_populates_all_domains(self):
        """Seeded rows appear in every domain the command claims to fill."""
        self._run()
        self.assertEqual(
            Product.objects.filter(slug__startswith="loadtest-").count(), 20
        )
        self.assertEqual(Product.objects.filter(sku__startswith="LT-").count(), 20)
        self.assertEqual(Order.objects.filter(phone__startswith="+25480").count(), 50)
        self.assertEqual(
            OrderItem.objects.filter(product_sku__startswith="LT-").count() > 0,
            True,
        )
        self.assertEqual(
            ProductViewEvent.objects.filter(session_key__startswith="lt-sess-").count(),
            40,
        )

    def test_re_run_replaces_not_duplicates(self):
        """Running twice leaves exactly one full seed, not two."""
        self._run()
        self._run()
        self.assertEqual(
            Product.objects.filter(slug__startswith="loadtest-").count(), 20
        )
        self.assertEqual(Order.objects.filter(phone__startswith="+25480").count(), 50)
        self.assertEqual(
            ProductViewEvent.objects.filter(session_key__startswith="lt-sess-").count(),
            40,
        )

    def test_wipe_only_removes_seeded_rows(self):
        """``--wipe-only`` leaves the database clean of load-test markers."""
        self._run()
        self._run(wipe_only=True)
        self.assertEqual(
            Product.objects.filter(slug__startswith="loadtest-").count(), 0
        )
        self.assertEqual(Product.objects.filter(sku__startswith="LT-").count(), 0)
        self.assertEqual(Order.objects.filter(phone__startswith="+25480").count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(
            ProductViewEvent.objects.filter(session_key__startswith="lt-sess-").count(),
            0,
        )


class CloudinaryMediaStorageTests(SimpleTestCase):
    """The Cloudinary storage routes each file to its own resource type."""

    def setUp(self):
        self._previous_cloud_name = cloudinary.config().cloud_name
        cloudinary.config(cloud_name="demo")

    def tearDown(self):
        cloudinary.config(cloud_name=self._previous_cloud_name)

    def test_images_upload_as_image_resource_type(self):
        """Image extensions resolve to the image resource type."""
        storage = CloudinaryMediaStorage()
        for name in (
            "products/images/photo.jpg",
            "products/images/photo.JPEG",
            "products/images/photo.png",
            "products/images/photo.webp",
            "products/images/photo.avif",
        ):
            self.assertEqual(storage._get_resource_type(name), "image")

    def test_pdfs_and_unknown_files_upload_as_raw(self):
        """PDFs and anything unrecognized resolve to the raw resource type."""
        storage = CloudinaryMediaStorage()
        self.assertEqual(
            storage._get_resource_type("products/manuals/guide.pdf"), "raw"
        )
        self.assertEqual(
            storage._get_resource_type("support/attachments/note.txt"), "raw"
        )
        self.assertEqual(
            storage._get_resource_type("products/images/no-extension"), "raw"
        )

    def test_videos_upload_as_video_resource_type(self):
        """Video extensions resolve to the video resource type."""
        storage = CloudinaryMediaStorage()
        self.assertEqual(storage._get_resource_type("products/video/demo.mp4"), "video")

    @mock.patch("cloudinary_storage.storage.cloudinary.uploader.upload")
    def test_save_sends_image_with_image_resource_type(self, mock_upload):
        """Saving a photo calls the upload API with resource_type=image."""
        mock_upload.return_value = {"public_id": "products/images/photo"}
        name = CloudinaryMediaStorage().save(
            "products/images/photo.jpg", ContentFile(b"data")
        )
        self.assertEqual(name, "products/images/photo")
        self.assertEqual(mock_upload.call_args.kwargs["resource_type"], "image")

    @mock.patch("cloudinary_storage.storage.cloudinary.uploader.upload")
    def test_save_sends_pdf_with_raw_resource_type(self, mock_upload):
        """Saving a manual calls the upload API with resource_type=raw."""
        mock_upload.return_value = {"public_id": "products/manuals/guide"}
        name = CloudinaryMediaStorage().save(
            "products/manuals/guide.pdf", ContentFile(b"%PDF-1.4")
        )
        self.assertEqual(name, "products/manuals/guide")
        self.assertEqual(mock_upload.call_args.kwargs["resource_type"], "raw")

    def test_url_builds_a_cloudinary_delivery_url_offline(self):
        """URL building needs no network and points at the stored public id."""
        url = CloudinaryMediaStorage().url("products/images/photo")
        self.assertIn("res.cloudinary.com/demo", url)
        self.assertIn("products/images/photo", url)
