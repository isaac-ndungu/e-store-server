"""API views for the core app.

Both endpoints are deliberately public and read-only: the storefront fetches
branding, theme, currency, payment methods and the VAT rate before any user is
authenticated. No secrets live in ``SiteConfig`` — sensitive values stay in
``config/settings.py`` / the environment — so exposing this record width is
safe.
"""

from rest_framework import generics, permissions
from rest_framework.throttling import ScopedRateThrottle

from apps.core.selectors import get_site_config
from apps.core.serializers import (
    LegalInformationSerializer,
    SiteConfigSerializer,
)


class SiteConfigView(generics.RetrieveAPIView):
    """Return the full public site configuration for the storefront.

    Public (``AllowAny``) by design — the catalog/checkout clients need the
    currency, payment methods, and feature toggles on first load, before login.
    Rate-limited with the shared ``public`` scope.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = SiteConfigSerializer

    def get_object(self):
        """Return the singleton config row regardless of URL parameters."""
        return get_site_config()


class LegalInformationView(generics.RetrieveAPIView):
    """Return the legal / trust copy (KRA PIN, return policy, physical address).

    Public (``AllowAny``) by design — consumer-law disclosures must be visible
    without an account. Rate-limited with the shared ``public`` scope.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = LegalInformationSerializer

    def get_object(self):
        """Return the singleton config row regardless of URL parameters."""
        return get_site_config()
