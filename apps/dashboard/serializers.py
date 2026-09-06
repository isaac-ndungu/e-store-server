"""API serializers for the dashboard app.

The dashboard is read-only and every payload is a computed dict, so there are
no ``ModelSerializer`` outputs — the serializers here exist to whitelist and
validate query parameters. Each view resolves its query string through one of
these classes before touching the database, mirroring the analytics app's
parameter handling: an explicit field list, unknown parameters rejected, and
``from``/``to`` mapped onto ``start``/``end`` on the wire.
"""

from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import serializers


class _OptionalDateTimeField(serializers.DateTimeField):
    """A datetime field that also accepts a bare ``YYYY-MM-DD`` date.

    Widget clients commonly send a date where a datetime is meant. The field
    accepts both the ISO date and full datetime forms so a plain date starts
    at midnight for that day. Naive values are made timezone-aware in the
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


class DashboardQuerySerializer(serializers.Serializer):
    """Period filter shared by the period-bound dashboard widgets.

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


class ProductsQuerySerializer(DashboardQuerySerializer):
    """Period filter plus the ``limit`` cap for the products widget."""

    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)
