"""Cross-app concurrency tests for race-prone money and stock paths.

These exercises run true parallel database writers against PostgreSQL. The
default in-memory SQLite test database serialises every query behind one
connection and cannot genuinely race ``select_for_update`` locks, so the
whole module is skipped when the test database is not PostgreSQL. CI sets
``TEST_USE_POSTGRES`` to run them, and they can be run locally with
``TEST_USE_POSTGRES=True python manage.py test apps.audit``.

The two scenarios modelled are the ones that matter under real checkout
load: ten shoppers competing for the last units of a variant, and Safaricom
resending the same STK callback twice within the same instant.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import mock, skipUnless

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.cart.services import add_item, get_or_create_cart
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inventory.models import Inventory, SerialUnit, StockReservation, Warehouse
from apps.orders.models import OrderStatusHistory
from apps.orders.services import create_order_from_cart
from apps.payments.models import MpesaTransaction, Payment
from apps.payments.services import handle_stk_callback
from apps.shipping.models import DeliveryZone

_RESERVE = "reserve"
_REJECT = "reject"


def _postgres_only(func):
    """Skip a test unless the test database is PostgreSQL."""
    return skipUnless(
        connection.vendor == "postgresql", "These tests require PostgreSQL."
    )(func)


def _mock_sms():
    """Return a context manager stubbing the SMS provider to report success."""
    return mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "test-sms-id",
            "response": {},
            "segments": 1,
            "error": "",
        },
    )


class ReservationRaceTests(TransactionTestCase):
    """Parallel reservation creation must not over-commit scarce stock."""

    reset_sequences = True

    def setUp(self):
        """Start each test from a clean cache."""
        cache.clear()

    def _make_stock(self, quantity):
        """Create a product, one variant, and one warehouse with stock."""
        category, _ = Category.objects.get_or_create(name="Kitchen", slug="kitchen")
        brand, _ = Brand.objects.get_or_create(name="Ramtons", slug="ramtons")
        product = Product.objects.create(
            name="Race Blender",
            slug="race-blender",
            sku="RACE-1",
            description="A test product.",
            brand=brand,
            is_active=True,
            tracks_serial_numbers=False,
        )
        variant = ProductVariant.objects.create(
            product=product,
            sku="RACE-1-V",
            is_active=True,
            price=Decimal("5000.00"),
        )
        warehouse = Warehouse.objects.create(name="Nairobi WH")
        Inventory.objects.create(
            variant=variant, warehouse=warehouse, quantity=quantity
        )
        return variant

    @_postgres_only
    def test_concurrent_reservations_never_exceed_stock(self):
        """Ten shoppers racing for 5 units must yield exactly five holds."""
        from apps.inventory.services import create_reservation

        variant = self._make_stock(quantity=5)
        barrier = Barrier(10)
        outcomes = []

        def reserve_one(_index):
            """Attempt one reservation once the whole group is ready."""
            from django.db import connections

            barrier.wait()
            try:
                create_reservation(variant=variant, quantity=1)
                outcomes.append(_RESERVE)
            except ValidationError as exc:
                outcomes.append(f"{_REJECT}:{exc}")
            finally:
                connections.close_all()

        worker_count = 10
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(reserve_one, range(worker_count)))

        self.assertEqual(sum(o == _RESERVE for o in outcomes), 5, outcomes)
        self.assertEqual(sum(o.startswith(_REJECT) for o in outcomes), 5, outcomes)
        self.assertTrue(
            all(
                outcome.startswith(_REJECT)
                for outcome in outcomes
                if outcome != _RESERVE
            ),
            outcomes,
        )

        inventory = Inventory.objects.get(variant=variant)
        self.assertEqual(inventory.quantity, 5)
        self.assertEqual(inventory.reserved, 5)
        self.assertEqual(StockReservation.objects.filter(status="active").count(), 5)


class StkCallbackRaceTests(TransactionTestCase):
    """A duplicated STK callback must confirm the order exactly once."""

    reset_sequences = True

    def setUp(self):
        """Build a stocked product and a pending order tied to a callback."""
        cache.clear()
        self.user = User.objects.create_user(
            email="payer@example.com",
            username="payer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        category, _ = Category.objects.get_or_create(name="Kitchen", slug="kitchen")
        brand, _ = Brand.objects.get_or_create(name="Ramtons", slug="ramtons")
        product = Product.objects.create(
            name="Fan",
            slug="fan",
            sku="FAN-1",
            description="A test product.",
            brand=brand,
            is_active=True,
            tracks_serial_numbers=False,
        )
        self.variant = ProductVariant.objects.create(
            product=product,
            sku="FAN-1-V",
            is_active=True,
            price=Decimal("3000.00"),
            package_weight=Decimal("2.00"),
        )
        self.warehouse = Warehouse.objects.create(name="Nairobi WH")
        Inventory.objects.create(
            variant=self.variant, warehouse=self.warehouse, quantity=5
        )
        self.zone = DeliveryZone.objects.create(
            county="Nairobi",
            area_name="CBD",
            base_fee=Decimal("200.00"),
            per_kg_rate=Decimal("40.00"),
        )
        self._create_order_and_txn()

    def _create_order_and_txn(self):
        """Create a pending M-Pesa order with a matching pending transaction."""
        cart = get_or_create_cart(user=self.user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=self.user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        self.order = order
        self.txn = MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_RACE_001",
            status="pending",
        )

    def _success_callback(self):
        """Return a Daraja success callback body for the race transaction.

        The amount returned mirrors the actual charge so the callback takes
        the confirm path rather than the amount-mismatch path.
        """
        return {
            "Body": {
                "StkCallback": {
                    "MerchantRequestID": "mr-race-001",
                    "CheckoutRequestID": "ws_CO_RACE_001",
                    "ResultCode": "0",
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": str(self.order.grand_total)},
                            {"Name": "MpesaReceiptNumber", "Value": "RACERCPT001"},
                            {"Name": "TransactionDate", "Value": 20260101120000},
                            {"Name": "PhoneNumber", "Value": 254712345678},
                        ]
                    },
                }
            }
        }

    @_postgres_only
    def test_duplicate_callback_confirms_order_once(self):
        """Two simultaneous callbacks must not double-confirm or double-deduct."""
        barrier = Barrier(2)

        def fire_callback(_index):
            """Fire the same callback body behind the start barrier."""
            from django.db import connections

            barrier.wait()
            try:
                return handle_stk_callback(self._success_callback())
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(fire_callback, range(2)))

        self.txn.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.txn.status, "success")
        self.assertEqual(self.txn.mpesa_receipt_number, "RACERCPT001")
        self.assertEqual(self.order.status, "confirmed")

        self.assertEqual(Payment.objects.count(), 1)
        confirmations = OrderStatusHistory.objects.filter(
            order=self.order, to_status="confirmed"
        ).count()
        self.assertEqual(confirmations, 1)

        inventory = Inventory.objects.get(variant=self.variant)
        self.assertEqual(inventory.quantity, 4)
        self.assertEqual(inventory.reserved, 0)

        reservations = StockReservation.objects.filter(
            order_item__order=self.order, status="fulfilled"
        )
        self.assertEqual(reservations.count(), 1)


class SerialReservationRaceTests(TransactionTestCase):
    """Serial-unit ownership must not drift from the inventory counts.

    For serialized products the unit ``status`` and the sibling ``Inventory``
    counts move together: reserving marks units ``reserved`` and raises the
    count, fulfilling marks them ``sold`` and drops both counts. This races
    that invariant — over-selling must be rejected exactly at stock level, the
    reserved count must equal the units actually held, and fulfilment must
    reconcile both sides.
    """

    reset_sequences = True

    def setUp(self):
        """Start each test from a clean cache."""
        cache.clear()

    def _make_serialised_stock(self, quantity):
        """Create a serialized variant and register ``quantity`` serial units."""
        from apps.inventory.services import receive_serial_units

        category, _ = Category.objects.get_or_create(name="Kitchen", slug="kitchen")
        brand, _ = Brand.objects.get_or_create(name="Ramtons", slug="ramtons")
        product = Product.objects.create(
            name="Serial Race Cooker",
            slug="serial-race-cooker",
            sku="SRACE-1",
            description="A serialized test product.",
            brand=brand,
            is_active=True,
            tracks_serial_numbers=True,
        )
        variant = ProductVariant.objects.create(
            product=product,
            sku="SRACE-1-V",
            is_active=True,
            price=Decimal("9000.00"),
        )
        warehouse = Warehouse.objects.create(name="Nairobi WH")
        receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=[f"SRACE-{index:03d}" for index in range(quantity)],
        )
        return variant, warehouse

    @_postgres_only
    def test_concurrent_serialised_reservations_reconcile_units_and_counts(self):
        """Ten shoppers for 5 serialized units hold exactly five units."""
        from apps.inventory.services import create_reservation, fulfill_reservation

        variant, warehouse = self._make_serialised_stock(quantity=5)
        barrier = Barrier(10)
        outcomes = []

        def reserve_one(_index):
            """Attempt one reservation once the whole group is ready."""
            from django.db import connections

            barrier.wait()
            try:
                create_reservation(variant=variant, quantity=1, warehouse=warehouse)
                outcomes.append(_RESERVE)
            except ValidationError as exc:
                outcomes.append(f"{_REJECT}:{exc}")
            finally:
                connections.close_all()

        worker_count = 10
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(reserve_one, range(worker_count)))

        self.assertEqual(sum(o == _RESERVE for o in outcomes), 5, outcomes)
        self.assertEqual(sum(o.startswith(_REJECT) for o in outcomes), 5, outcomes)

        inventory = Inventory.objects.get(variant=variant)
        self.assertEqual(inventory.quantity, 5)
        self.assertEqual(inventory.reserved, 5)

        reservations = StockReservation.objects.filter(status="active")
        self.assertEqual(reservations.count(), 5)
        reserved_units = SerialUnit.objects.filter(variant=variant, status="reserved")
        in_stock_units = SerialUnit.objects.filter(variant=variant, status="in_stock")
        self.assertEqual(reserved_units.count(), 5)
        self.assertEqual(in_stock_units.count(), 0)
        self.assertEqual(
            set(reserved_units.values_list("reservation_id", flat=True)),
            set(reservations.values_list("pk", flat=True)),
        )

        for reservation in list(reservations):
            fulfill_reservation(reservation=reservation)

        inventory.refresh_from_db()
        self.assertEqual(inventory.reserved, 0)
        self.assertEqual(inventory.quantity, 0)
        self.assertEqual(
            SerialUnit.objects.filter(variant=variant, status="sold").count(), 5
        )
        self.assertEqual(
            SerialUnit.objects.filter(variant=variant, status="reserved").count(), 0
        )


class OrderPlacementIdempotencyRaceTests(TransactionTestCase):
    """A replayed 'place order' tap must not create a second order.

    The outbound idempotency contract: two simultaneous requests carrying the
    same ``Idempotency-Key`` for the same caller must produce exactly one
    order. One request wins the processing lock and places the order; the
    other either observes the lock (409) or replays the cached result (same
    201 payload). Either way only one order, one reservation, and one unit
    of reserved stock may ever exist.
    """

    reset_sequences = True

    def setUp(self):
        """Build a stocked product, a buyer, and a filled cart."""
        cache.clear()
        self.user = User.objects.create_user(
            email="repeat-buyer@example.com",
            username="repeat-buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        category, _ = Category.objects.get_or_create(name="Kitchen", slug="kitchen")
        brand, _ = Brand.objects.get_or_create(name="Ramtons", slug="ramtons")
        product = Product.objects.create(
            name="Idempotent Kettle",
            slug="idempotent-kettle",
            sku="KETTLE-1",
            description="A test product.",
            brand=brand,
            is_active=True,
            tracks_serial_numbers=False,
        )
        self.variant = ProductVariant.objects.create(
            product=product,
            sku="KETTLE-1-V",
            is_active=True,
            price=Decimal("2500.00"),
            package_weight=Decimal("1.50"),
        )
        self.warehouse = Warehouse.objects.create(name="Nairobi WH")
        Inventory.objects.create(
            variant=self.variant, warehouse=self.warehouse, quantity=5
        )
        self.zone = DeliveryZone.objects.create(
            county="Nairobi",
            area_name="CBD",
            base_fee=Decimal("200.00"),
            per_kg_rate=Decimal("40.00"),
        )
        self.cart = get_or_create_cart(user=self.user)
        add_item(self.cart, variant_id=self.variant.pk, quantity=1)

    def _order_payload(self):
        """Return the minimal order-placement payload."""
        return {
            "phone": "+254712345678",
            "payment_method": "cod",
            "delivery_zone_id": self.zone.pk,
        }

    @_postgres_only
    def test_duplicate_place_order_tap_creates_one_order(self):
        """Two same-key placements yield one order and one reservation."""
        from rest_framework.test import APIClient

        _ = self  # silence unused-self linters; setup state used below
        barrier = Barrier(2)
        outcomes = []

        def place_order(_index):
            """Fire an identical order placement behind the start barrier."""
            from django.db import connections

            client = APIClient()
            client.force_authenticate(user=self.user)
            barrier.wait()
            try:
                with _mock_sms():
                    response = client.post(
                        "/api/v1/orders/",
                        self._order_payload(),
                        format="json",
                        HTTP_IDEMPOTENCY_KEY="place-order-race-key",
                    )
                    outcomes.append(response.status_code)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(place_order, range(2)))

        self.assertEqual(self.user.orders.count(), 1)
        self.assertIn(201, outcomes)
        self.assertTrue(set(outcomes) <= {201, 409}, outcomes)

        inventory = Inventory.objects.get(variant=self.variant)
        self.assertEqual(inventory.reserved, 1)
        self.assertEqual(StockReservation.objects.filter(status="active").count(), 1)
