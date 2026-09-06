"""API serializers for the analytics app.

Analytics is read-only, so there are no write serializers and no writable
fields. ``AnalyticsQuerySerializer`` whitelists the period parameters every
report accepts — an optional ISO date/datetime ``start``/``end`` pair (exposed
on the wire as ``from``/``to``) and an optional ``group_by`` / ``limit`` where a
report supports them — and rejects everything else. Non-aggregate output is
shaped by hand rather than through ``ModelSerializer`` because every payload
is a computed dict, not a row.
"""

from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import serializers


class _OptionalDateTimeField(serializers.DateTimeField):
    """A datetime field that also accepts a bare ``YYYY-MM-DD`` date.

    Dashboard clients commonly send a date where a datetime is meant. The field
    accepts both the ISO date and full datetime forms so a plain date starts at
    midnight for that day, matching the inclusive/latest-end semantics the
    reports apply to their bounds. Naive values are made timezone-aware in the
    configured timezone so a date-only bound never compares unawares against
    the database.
    """

    def to_internal_value(self, value):
        """Parse an ISO date or datetime, merging day-level dates to midnight.

        Args:
            value: the raw query-string value.

        Returns:
            datetime: the parsed, timezone-aware datetime.
        """
        parsed_date = parse_date(value)
        if parsed_date is not None:
            parsed = datetime.combine(parsed_date, time.min)
        else:
            parsed = parse_datetime(value)
            if parsed is None:
                raise serializers.ValidationError(
                    "Use an ISO date (YYYY-MM-DD) or datetime " "(YYYY-MM-DDTHH:MM:SS)."
                )
        if parsed.tzinfo is None:
            parsed = timezone.make_aware(parsed)
        return parsed


class AnalyticsQuerySerializer(serializers.Serializer):
    """Shared period filter for analytics reports.

    ``start`` and ``end`` are both optional; omitting them means "all time".
    The wire parameters are ``from``/``to`` — the view maps them onto these
    fields because ``from`` is a reserved word and cannot name a serializer
    field.
    """

    start = _OptionalDateTimeField(required=False)
    end = _OptionalDateTimeField(required=False)

    def validate(self, attrs):
        """Reject a reversed period.

        Args:
            attrs (dict): the validated field values.

        Returns:
            dict: the validated values.

        Raises:
            serializers.ValidationError: when ``start`` is later than ``end``.
        """
        start = attrs.get("start")
        end = attrs.get("end")
        if start is not None and end is not None and start > end:
            raise serializers.ValidationError({"end": "to must be on or after from."})
        return attrs


class SalesQuerySerializer(AnalyticsQuerySerializer):
    """Period filter plus the ``group_by`` bucket dimension for the sales report."""

    group_by = serializers.ChoiceField(
        choices=("day", "week", "month"), required=False, default="day"
    )


class ProductPerformanceQuerySerializer(AnalyticsQuerySerializer):
    """Period filter plus the ``limit`` cap for the product report."""

    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)


class SummarySerializer(serializers.Serializer):
    """Read-only framing serializer for the assembled dashboard summary.

    Declared for schema documentation; the payload itself is a computed dict,
    so no fields are read off a model.
    """

    period = serializers.DictField()
    sales = serializers.DictField()
    orders_by_status = serializers.ListField()
    stock = serializers.DictField()
    catalogue = serializers.DictField()
    products = serializers.ListField()
    traffic = serializers.DictField()
    promotions = serializers.DictField()
    returns = serializers.DictField()
    support = serializers.DictField()
    reviews = serializers.DictField()
    notifications = serializers.DictField()
