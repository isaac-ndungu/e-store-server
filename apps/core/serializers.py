"""Serializers for the core app.

Both endpoints are read-only and public (the storefront needs site config and
legal copy before a user is authenticated), so the serializers declare an
explicit readable field list and mark every field read-only — there are no
writable fields on either endpoint.
"""

from rest_framework import serializers

from apps.core.models import SiteConfig


class SiteConfigSerializer(serializers.ModelSerializer):
    """Full public site configuration (branding, theme, VAT rate, toggles)."""

    class Meta:
        model = SiteConfig
        fields = [
            "site_name",
            "tagline",
            "domain",
            "logo",
            "favicon",
            "theme",
            "settings",
            "contact_email",
            "support_phone",
            "social_links",
            "kra_pin",
            "business_registration_number",
            "physical_address",
            "return_policy_text",
            "cooling_off_period_days",
            "standard_vat_rate",
        ]
        read_only_fields = fields


class LegalInformationSerializer(serializers.ModelSerializer):
    """Legal and trust copy the storefront must render (consumer-law fields)."""

    class Meta:
        model = SiteConfig
        fields = [
            "site_name",
            "domain",
            "contact_email",
            "support_phone",
            "kra_pin",
            "business_registration_number",
            "physical_address",
            "return_policy_text",
            "cooling_off_period_days",
        ]
        read_only_fields = fields
