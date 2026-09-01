"""Tests for the health endpoint.

A smoke test so CI (and Postman) can confirm the backend serves requests.
"""

from django.test import SimpleTestCase
from django.urls import reverse


class HealthEndpointTests(SimpleTestCase):
    """Verify the liveness probe responds correctly."""

    def test_health_returns_ok(self):
        """The health endpoint reports the backend is up and reachable."""
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_api_v1_mount_is_accessible(self):
        """The /api/v1/ mount exists and responds, fail-closed by default.

        The DRF API root is authenticated by default (global default permission
        is ``IsAuthenticated``), so an anonymous caller receives 401 rather than
        404 — proving the mount resolves without exposing anything publicly.
        """
        response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 401)
