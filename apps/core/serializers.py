"""Serializers for the core app.

Reads are public (the storefront needs site config and legal copy before a
user is authenticated), so the read serializers declare an explicit readable
field list and mark every field read-only. Writes go through a separate
update serializer with its own explicit writable list: only storefront
contact facts are staff-editable over the API, while VAT, legal copy, and
the JSON blobs stay Django-admin-only.
"""

import re

from rest_framework import serializers

from apps.core.models import SiteConfig

WHATSAPP_DIGITS = re.compile(r"^\+?[0-9]{7,15}$")


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
            "whatsapp_number",
            "order_intake_email",
            "facebook_url",
            "instagram_url",
            "tiktok_url",
            "social_links",
            "kra_pin",
            "business_registration_number",
            "physical_address",
            "return_policy_text",
            "cooling_off_period_days",
            "standard_vat_rate",
        ]
        read_only_fields = fields


class SiteConfigUpdateSerializer(serializers.ModelSerializer):
    """Staff-editable site-config fields for the console settings form.

    Only storefront contact facts are writable here: the WhatsApp number and
    intake email drive the cart hand-offs, the social profile URLs drive the
    footer links, and the remaining fields are the matching public contact
    copy. VAT, KRA/legal copy, theme, settings, and media stay
    Django-admin-only so a console session cannot touch money or
    compliance data.
    """

    class Meta:
        model = SiteConfig
        fields = [
            "site_name",
            "tagline",
            "contact_email",
            "support_phone",
            "whatsapp_number",
            "order_intake_email",
            "facebook_url",
            "instagram_url",
            "tiktok_url",
        ]

    def validate_whatsapp_number(self, value):
        """Accept a blank number or an international digit string.

        Args:
            value: the submitted WhatsApp number.

        Returns:
            str: the trimmed number.

        Raises:
            serializers.ValidationError: when the value is not blank and does
                not look like an international phone number.
        """
        number = (value or "").strip().replace(" ", "")
        if number == "":
            return ""
        if not WHATSAPP_DIGITS.match(number):
            raise serializers.ValidationError(
                "Enter a valid international number, e.g. 254712345678."
            )
        return number


class LegalInformationSerializer(serializers.ModelSerializer):
    """Legal and trust copy the storefront must render (consumer-law fields)."""

    class Meta:
        model = SiteConfig
        fields = [
            "site_name",
            "domain",
            "contact_email",
            "support_phone",
            "whatsapp_number",
            "order_intake_email",
            "kra_pin",
            "business_registration_number",
            "physical_address",
            "return_policy_text",
            "cooling_off_period_days",
        ]
        read_only_fields = fields
