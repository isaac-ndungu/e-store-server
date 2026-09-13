"""Concurrency probes for the assisted-sales paths.

These tests exercise row-locking under real database concurrency and only
run on PostgreSQL (SQLite cannot do concurrent writes). Each probe races
the operation that must be exactly-once:

- duplicate intake POSTs with one idempotency key (one order total),
- concurrent pre-shipment cancels (exactly one cancellation is recorded),
- concurrent manual-refund records (the refund is recorded exactly once).
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.orders.models import Order, OrderStatusHistory
from apps.orders.services import create_staff_order
from apps.returns.models import ReturnRequestStatusHistory
from apps.returns.services import (
    approve_return_request,
    cancel_confirmed_order,
    create_return_request,
    record_item_received,
    refund_return_request,
)

_REFUND_METHOD = "M-Pesa - sent manually"


def _postgres_only(func):
    """Skip the test unless the suite runs against PostgreSQL."""
    return skipUnless(
        connection.vendor == "postgresql", "These tests require PostgreSQL."
    )(func)


def _make_staff(suffix="race"):
    """Create a staff user for intake races."""
    return User.objects.create_user(
        email=f"{suffix}@example.com",
        username=f"user-{suffix}",
        password="StrongPass123!",
        phone_number="+254700000001",
        role="support",
    )


def _make_variant(sku="RACE-1"):
    """Create a product with one active variant and no stock behind it."""
    category, _ = Category.objects.get_or_create(name="Kitchen", slug="kitchen")
    brand, _ = Brand.objects.get_or_create(name="Ramtons", slug="ramtons")
    product = Product.objects.create(
        name="Race Blender",
        slug="race-blender",
        sku=sku,
        description="A test product.",
        brand=brand,
        is_active=True,
    )
    return ProductVariant.objects.create(
        product=product,
        sku=f"{sku}-V",
        is_active=True,
        price=Decimal("5000.00"),
    )


class IntakeIdempotencyRaceTests(TransactionTestCase):
    """A replayed intake tap must not create a second order.

    The outbound idempotency contract: two simultaneous requests carrying the
    same ``Idempotency-Key`` for the same staff caller must produce exactly
    one order. One request wins the processing lock and creates the order;
    the other either observes the lock (409) or replays the cached result
    (same 201 payload). Either way only one order may ever exist.
    """

    reset_sequences = True

    def setUp(self):
        """Build a product and a staff caller."""
        cache.clear()
        self.staff = _make_staff("intake-race")
        self.variant = _make_variant("KETTLE-1")

    def _intake_payload(self):
        """Return the minimal intake payload."""
        return {
            "phone": "+254712345678",
            "order_source": "whatsapp",
            "payment_method": "cod",
            "items": [{"variant_id": self.variant.pk, "quantity": 1}],
        }

    @_postgres_only
    def test_duplicate_intake_tap_creates_one_order(self):
        """Two same-key intakes yield one order."""
        from rest_framework.test import APIClient

        barrier = Barrier(2)
        outcomes = []

        def place_order(_index):
            """Fire an identical intake behind the start barrier."""
            from django.db import connections

            client = APIClient()
            client.force_authenticate(user=self.staff)
            barrier.wait()
            try:
                response = client.post(
                    "/api/v1/orders/intake/",
                    self._intake_payload(),
                    format="json",
                    HTTP_IDEMPOTENCY_KEY="intake-race-key",
                )
                outcomes.append(response.status_code)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(place_order, range(2)))

        self.assertEqual(Order.objects.count(), 1)
        self.assertIn(201, outcomes)
        self.assertTrue(set(outcomes) <= {201, 409}, outcomes)

        confirmations = OrderStatusHistory.objects.filter(to_status="confirmed").count()
        self.assertEqual(confirmations, 1)


class CancelRaceTests(TransactionTestCase):
    """Concurrent cancels must record exactly one cancellation."""

    reset_sequences = True

    def setUp(self):
        """Build a confirmed order and a staff caller."""
        cache.clear()
        self.staff = _make_staff("cancel-race")
        variant = _make_variant("CANCEL-1")
        self.order = create_staff_order(
            staff_user=self.staff,
            phone="+254712345678",
            lines=[{"variant_id": variant.pk, "quantity": 1}],
            order_source="whatsapp",
            payment_method="cod",
        )

    @_postgres_only
    def test_concurrent_cancels_record_one_cancellation(self):
        """Two simultaneous cancels yield one cancelled order, one audit row."""
        barrier = Barrier(2)
        outcomes = []

        def cancel(_index):
            """Fire a cancellation behind the start barrier."""
            from django.db import connections

            barrier.wait()
            try:
                cancel_confirmed_order(
                    order=self.order,
                    user=self.staff,
                    note="Customer asked to cancel.",
                )
                outcomes.append("cancelled")
            except ValidationError:
                outcomes.append("rejected")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(cancel, range(2)))

        self.assertEqual(outcomes.count("cancelled"), 1, outcomes)
        self.assertEqual(outcomes.count("rejected"), 1, outcomes)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(
            OrderStatusHistory.objects.filter(to_status="cancelled").count(), 1
        )


class RefundRecordRaceTests(TransactionTestCase):
    """Concurrent refund records must settle the return exactly once."""

    reset_sequences = True

    def setUp(self):
        """Build a delivered order with an approved, received return."""
        cache.clear()
        self.staff = _make_staff("refund-race")
        variant = _make_variant("REFUND-1")
        order = create_staff_order(
            staff_user=self.staff,
            phone="+254712345678",
            lines=[{"variant_id": variant.pk, "quantity": 1}],
            order_source="whatsapp",
            payment_method="cod",
        )
        from apps.orders.services import transition_order

        transition_order(order, "shipped")
        transition_order(order, "delivered")
        self.return_request = create_return_request(
            order=order, reason="faulty", user=self.staff
        )
        approve_return_request(
            return_request=self.return_request,
            refund_method=_REFUND_METHOD,
            user=self.staff,
        )
        record_item_received(return_request=self.return_request, user=self.staff)

    @_postgres_only
    def test_concurrent_refund_records_settle_once(self):
        """Two simultaneous refund records yield one refunded request."""
        barrier = Barrier(2)
        outcomes = []

        def record(_index):
            """Fire a refund record behind the start barrier."""
            from django.db import connections

            barrier.wait()
            try:
                refund_return_request(
                    return_request=self.return_request,
                    refund_note="M-Pesa sent",
                    user=self.staff,
                )
                outcomes.append("refunded")
            except ValidationError:
                outcomes.append("rejected")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(record, range(2)))

        self.assertEqual(outcomes.count("refunded"), 2, outcomes)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "refunded")
        self.assertEqual(
            ReturnRequestStatusHistory.objects.filter(to_status="refunded").count(),
            1,
        )
