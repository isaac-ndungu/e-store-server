"""Tests for the shipping app.

Covers the public delivery-area list (anonymous allowed, active-only, county
filter) and the admin area CRUD (auth + staff gating, county validation,
uniqueness). There is deliberately no quote endpoint to test — delivery cost
is quoted by staff and entered at order intake.
"""

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.shipping.models import DeliveryArea

LIST_URL = reverse("api:shipping:delivery-areas")
ADMIN_LIST_URL = reverse("api:shipping:admin-delivery-area-list-create")


def _make_user(email, role="manager"):
    """Create a staff user with the given role."""
    return User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254712345678",
        is_staff=True,
        role=role,
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
