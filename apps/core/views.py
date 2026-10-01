"""API views for the core app.

Reads are deliberately public: the storefront fetches branding, theme,
currency, payment methods and the VAT rate before any user is
authenticated. No secrets live in ``SiteConfig``  -  sensitive values stay in
``config/settings.py`` / the environment  -  so exposing this record width is
safe. Writes are staff-only and limited to the contact-fact fields the
update serializer whitelists.
"""

from rest_framework import generics, permissions
from rest_framework.throttling import ScopedRateThrottle

from apps.core.selectors import get_site_config
from apps.core.serializers import (
    LegalInformationSerializer,
    SiteConfigSerializer,
    SiteConfigUpdateSerializer,
)


class SiteConfigView(generics.RetrieveUpdateAPIView):
    """Read the public site configuration, update the contact facts as staff.

    ``GET`` stays public (``AllowAny``)  -  the catalog/checkout clients need
    the currency, payment methods, and feature toggles on first load, before
    login. ``PUT``/``PATCH`` require staff standing (``IsAdminUser``) and only
    touch the whitelisted contact fields; the singleton is addressed directly
    so there is no per-object lookup to authorize. Rate-limited with the
    shared ``public`` scope for reads and the ``admin`` scope for writes.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = SiteConfigSerializer
    http_method_names = ["get", "put", "patch", "head", "options"]

    def get_permissions(self):
        """Return public read access and staff-only write access.

        Returns:
            list: the permission instances for the current request method.
        """
        if self.request.method in ("PUT", "PATCH"):
            return [permissions.IsAdminUser()]
        return [permissions.AllowAny()]

    def get_throttles(self):
        """Rate-limit reads as public traffic and writes as admin traffic.

        Returns:
            list: the throttle instances for the current request method.
        """
        self.throttle_scope = (
            "admin" if self.request.method in ("PUT", "PATCH") else "public"
        )
        return super().get_throttles()

    def get_serializer_class(self):
        """Return the read or the whitelisted write serializer by method.

        Returns:
            type: the serializer class for the current request method.
        """
        if self.request.method in ("PUT", "PATCH"):
            return SiteConfigUpdateSerializer
        return SiteConfigSerializer

    def get_object(self):
        """Return the singleton config row regardless of URL parameters."""
        return get_site_config()


class LegalInformationView(generics.RetrieveAPIView):
    """Return the legal / trust copy (KRA PIN, return policy, physical address).

    Public (``AllowAny``) by design  -  consumer-law disclosures must be visible
    without an account. Rate-limited with the shared ``public`` scope.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"
    serializer_class = LegalInformationSerializer

    def get_object(self):
        """Return the singleton config row regardless of URL parameters."""
        return get_site_config()
