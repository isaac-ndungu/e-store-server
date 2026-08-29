"""Tests for the Step 0 health endpoint.

The only endpoint present before app-specific Steps begin; kept as a smoke test
so CI (and Postman) can confirm the backend serves requests from day one.
"""
from django.test import SimpleTestCase
from django.urls import reverse


class HealthEndpointTests(SimpleTestCase):
    """Verify the liveness probe responds correctly."""

    def test_health_returns_ok(self):
        """The health endpoint reports the backend is up and reachable."""
        response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_api_v1_mount_is_accessible(self):
        """The /api/v1/ mount exists and responds (currently empty router)."""
        response = self.client.get('/api/v1/')
        self.assertEqual(response.status_code, 200)
