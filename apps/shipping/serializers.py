"""Serializers for the shipping app.

The area serializer is flat and uses an explicit field list. Writes enforce
the county and uniqueness invariants so bad or ambiguous service-area data
never reaches the database. There is deliberately no fee schedule anywhere
in this app — delivery cost is quoted by staff and entered at order intake.
"""

from rest_framework import serializers

from apps.shipping.constants import is_valid_county
from apps.shipping.models import DeliveryArea


class DeliveryAreaSerializer(serializers.ModelSerializer):
    """Read/write serializer for delivery areas.

    Read responses expose the service-area list for the storefront picker;
    writes enforce county and uniqueness invariants.
    """

    class Meta:
        model = DeliveryArea
        fields = [
            "id",
            "county",
            "area_name",
            "is_active",
        ]
        read_only_fields = ["id"]

    def validate_county(self, value):
        """Reject a county that is not one of the 47 in Kenya.

        Args:
            value (str): the client-supplied county.

        Returns:
            str: the county unchanged.

        Raises:
            ValidationError: if the county does not match a known Kenyan county.
        """
        if not is_valid_county(value):
            raise serializers.ValidationError(
                "county must be one of Kenya's 47 counties."
            )
        return value

    def validate(self, attrs):
        """Reject a second area for the same county + area.

        The model enforces the pair as a uniqueness constraint; checking here
        turns the database error into a clean 400 for the admin caller. On
        updates only the fields actually present are compared, so a PATCH that
        leaves ``area_name`` untouched cannot collide with itself.

        Args:
            attrs (dict): the validated input fields.

        Returns:
            dict: the validated input fields unchanged.

        Raises:
            ValidationError: if the county + area already names an area.
        """
        instance = self.instance
        county = attrs.get("county", getattr(instance, "county", None))
        area_name = attrs.get("area_name", getattr(instance, "area_name", None))
        if county is not None and area_name is not None:
            duplicate = DeliveryArea.objects.filter(county=county, area_name=area_name)
            if instance is not None:
                duplicate = duplicate.exclude(pk=instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    {"area_name": "An area already exists for this county and area."}
                )
        return attrs
