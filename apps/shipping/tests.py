"""Tests for the shipping app.

Covers the public delivery-area list (anonymous allowed, active-only, county
filter) and the admin area CRUD (auth + staff gating, county validation,
uniqueness). There is deliberately no quote endpoint to test  -  delivery cost
is quoted by staff and entered at order intake.
"""

from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.shipping.constants import canonical_county
from apps.shipping.models import DeliveryArea
from apps.shipping.services import (
    build_delivery_area_pair,
    normalize_area_name,
    sync_delivery_areas,
)

LIST_URL = reverse("api:shipping:delivery-areas")
ADMIN_LIST_URL = reverse("api:shipping:admin-delivery-area-list-create")


def _make_user(email, is_staff=True):
    """Create a staff user for tests."""
    return User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254712345678",
        is_staff=is_staff,
    )


def _login(client, email):
    """Attach a JWT for the user to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(
        url, {"email": email, "password": "StrongPass123!"}, format="json"
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_area(county="Nairobi", area="Westlands", active=True):
    """Create a delivery area."""
    return DeliveryArea.objects.create(county=county, area_name=area, is_active=active)


class DeliveryAreaListTests(APITestCase):
    """Exercises the public area picker endpoint."""

    def setUp(self):
        cache.clear()

    def test_anonymous_list_allowed(self):
        """Anonymous visitors can read the area list."""
        _make_area()
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        area = response.data["results"][0]
        self.assertEqual(area["county"], "Nairobi")
        self.assertEqual(area["area_name"], "Westlands")
        self.assertNotIn("base_fee", area)

    def test_inactive_areas_hidden(self):
        """Deactivated areas are not offered to the storefront."""
        _make_area(active=False)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 0)

    def test_county_filter(self):
        """The list narrows to one county on request."""
        _make_area(county="Nairobi", area="Westlands")
        _make_area(county="Mombasa", area="Nyali")
        response = self.client.get(LIST_URL, {"county": "Mombasa"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["area_name"], "Nyali")


class DeliveryAreaAdminTests(APITestCase):
    """Exercises the admin area CRUD."""

    def setUp(self):
        cache.clear()
        _make_user("boss@example.com")
        _login(self.client, "boss@example.com")

    def _detail_url(self, pk):
        """Return the admin detail URL for an area."""
        return reverse("api:shipping:admin-delivery-area-detail", kwargs={"pk": pk})

    def test_admin_can_create_area(self):
        """Staff can add a service area."""
        response = self.client.post(
            ADMIN_LIST_URL,
            {"county": "Kisumu", "area_name": "Milimani", "is_active": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            DeliveryArea.objects.filter(county="Kisumu", area_name="Milimani").exists()
        )

    def test_admin_create_rejects_unknown_county(self):
        """Only Kenya's 47 counties are accepted."""
        response = self.client.post(
            ADMIN_LIST_URL,
            {"county": "Narnia", "area_name": "Cair Paravel"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_create_rejects_duplicate_area(self):
        """A county + area pair names exactly one area."""
        _make_area(county="Nairobi", area="Westlands")
        response = self.client.post(
            ADMIN_LIST_URL,
            {"county": "Nairobi", "area_name": "Westlands"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_can_deactivate_area(self):
        """Staff can take an area off the storefront."""
        area = _make_area()
        response = self.client.patch(
            self._detail_url(area.pk), {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        area.refresh_from_db()
        self.assertFalse(area.is_active)

    def test_anonymous_admin_access_rejected(self):
        """Anonymous callers cannot touch the admin CRUD."""
        self.client.credentials()
        response = self.client.get(ADMIN_LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PickupAreaSyncTests(APITestCase):
    """Exercises the courier pickup-point import into delivery areas."""

    def test_feed_names_normalize_to_canonical_values(self):
        """All-caps feed names resolve to official county and title-case area."""
        self.assertEqual(canonical_county("MURANGA "), "Murang'a")
        self.assertEqual(canonical_county("THARAKA NITHI"), "Tharaka-Nithi")
        self.assertEqual(normalize_area_name("MAKONGENI,THIKA"), "Makongeni, Thika")
        self.assertEqual(
            build_delivery_area_pair("LAIKIPIA", "NANYUKI TOWN"),
            ("Laikipia", "Nanyuki Town"),
        )

    def test_sync_creates_and_skips_unknown_counties(self):
        """Known pairs are created while blank or unknown counties are skipped."""
        result = sync_delivery_areas(
            [
                ("MOMBASA", "MOMBASA TOWN"),
                ("MOMBASA", "MOMBASA TOWN"),
                ("Narnia", "Cair Paravel"),
                ("", ""),
            ]
        )
        self.assertEqual(result, {"created": 1, "reactivated": 0, "skipped": 2})
        self.assertTrue(
            DeliveryArea.objects.filter(
                county="Mombasa", area_name="Mombasa Town"
            ).exists()
        )

    def test_sync_is_idempotent_and_reactivates(self):
        """A second run creates nothing and switches areas back on."""
        sync_delivery_areas([("Kiambu", "Thika Town")])
        area = DeliveryArea.objects.get(county="Kiambu", area_name="Thika Town")
        area.is_active = False
        area.save(update_fields=["is_active"])
        result = sync_delivery_areas([("KIAMBU", "THIKA TOWN")])
        self.assertEqual(result, {"created": 0, "reactivated": 1, "skipped": 0})
        area.refresh_from_db()
        self.assertTrue(area.is_active)

    def test_sync_command_loads_bundled_snapshot(self):
        """The management command populates areas without duplicates."""
        call_command("sync_delivery_areas")
        first_count = DeliveryArea.objects.count()
        self.assertGreater(first_count, 80)
        self.assertTrue(
            DeliveryArea.objects.filter(
                county="Kisumu", area_name="Kisumu Town (Andytech)"
            ).exists()
        )
        call_command("sync_delivery_areas")
        self.assertEqual(DeliveryArea.objects.count(), first_count)
