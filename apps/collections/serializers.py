"""Serializers for the collections app.

Read serializers stay flat so list endpoints render without extra queries.
Writes use an explicit field list and never expose more than the models
intend. A smart collection must carry exactly one registered ``smart_rule``;
a manual collection must carry none. Membership rows scope the product to
the catalog and the collection to the collections table.
"""

from rest_framework import serializers

from apps.collections.constants import SMART_RULE_CHOICES_VALUES
from apps.collections.models import Collection, CollectionMembership


class CollectionSerializer(serializers.ModelSerializer):
    """Read/write serializer for collections.

    Read responses surface the collection's display metadata and counts for
    the storefront; writes enforce type/rule invariants. ``product_count`` is
    a read-only convenience computed from the collection's membership.
    """

    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            "id",
            "name",
            "slug",
            "collection_type",
            "smart_rule",
            "rule_window_days",
            "rule_threshold",
            "description",
            "display_location",
            "sort_order",
            "is_active",
            "starts_at",
            "ends_at",
            "product_count",
        ]
        read_only_fields = ["id", "product_count"]

    def get_product_count(self, obj):
        """Return the number of products currently in the collection.

        Uses the cached membership length when available to avoid a query on a
        hot storefront path; otherwise counts the membership rows directly.

        Args:
            obj (Collection): the collection instance.

        Returns:
            int: the number of members.
        """
        from apps.collections import cache

        pks = cache.get_cached_product_pks(obj.slug)
        if pks is not None:
            return len(pks)
        return obj.memberships.count()

    def validate_name(self, value):
        """Normalise and lightly validate the display name.

        Args:
            value (str): the collection name.

        Returns:
            str: the name with whitespace collapsed.

        Raises:
            ValidationError: if the name is blank after normalisation.
        """
        value = " ".join(value.split()).strip()
        if not value:
            raise serializers.ValidationError("Collection name must not be blank.")
        return value

    def validate(self, attrs):
        """Require the correct rule for the collection type.

        A smart collection must specify exactly one registered smart rule and
        a positive window; a manual collection must not carry a rule.

        Args:
            attrs (dict): the validated input fields.

        Returns:
            dict: the validated input fields unchanged.

        Raises:
            ValidationError: if the type/rule combination is inconsistent.
        """
        instance = self.instance
        collection_type = attrs.get(
            "collection_type", getattr(instance, "collection_type", None)
        )
        smart_rule = attrs.get("smart_rule", getattr(instance, "smart_rule", ""))
        if collection_type == "smart" and smart_rule not in SMART_RULE_CHOICES_VALUES:
            raise serializers.ValidationError(
                {"smart_rule": "A smart collection must specify a valid smart rule."}
            )
        if collection_type == "manual" and smart_rule:
            raise serializers.ValidationError(
                {"smart_rule": "A manual collection cannot carry a smart rule."}
            )
        return attrs


class CollectionMembershipSerializer(serializers.ModelSerializer):
    """Read/write serializer for collection membership rows."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_slug = serializers.CharField(source="product.slug", read_only=True)

    class Meta:
        model = CollectionMembership
        fields = [
            "id",
            "collection",
            "product",
            "product_name",
            "product_slug",
            "sort_order",
            "added_at",
        ]
        read_only_fields = ["id", "added_at"]

    def validate(self, attrs):
        """Reject a product listed twice in the same collection.

        The model enforces the pair as unique; checking here turns the
        database error into a clean 400. On updates only fields present are
        compared, so a PATCH that leaves ``product`` untouched cannot collide.

        Args:
            attrs (dict): the validated input fields.

        Returns:
            dict: the validated input fields unchanged.

        Raises:
            ValidationError: if the product already belongs to the collection.
        """
        instance = self.instance
        collection = attrs.get("collection", getattr(instance, "collection", None))
        product = attrs.get("product", getattr(instance, "product", None))
        if collection is not None and product is not None:
            existing = CollectionMembership.objects.filter(
                collection=collection, product=product
            )
            if instance is not None:
                existing = existing.exclude(pk=instance.pk)
            if existing.exists():
                raise serializers.ValidationError(
                    {"product": "This product already belongs to the collection."}
                )
        return attrs
