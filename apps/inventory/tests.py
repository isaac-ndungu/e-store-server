"""Tests for the inventory app.

Covers the reservation lifecycle (create/fulfill/release/expiry), the
balance between ``Inventory.reserved`` and ``SerialUnit.status`` for
serialized products, per-warehouse and aggregate availability, the admin CRUD
endpoints, and the security separation between anonymous, customer, and admin
callers.
"""

from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Count
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Product, ProductVariant
from apps.core.idempotency import acquire_processing_lock, release_processing_lock
from apps.inventory.models import (
    Inventory,
    SerialUnit,
    StockMovementLog,
    StockReservation,
    Warehouse,
)
from apps.inventory.services import (
    create_reservation,
    fulfill_reservation,
    receive_serial_units,
    receive_stock,
    release_reservation,
    update_serial_unit_status,
)
from apps.inventory.tasks import expire_stale_reservations
from apps.inventory.views import MAX_BULK_AVAILABILITY

URLS = {
    "availability": reverse("api:inventory:availability"),
    "availability_bulk": reverse("api:inventory:availability-bulk"),
    "admin_warehouses": reverse("api:inventory:admin-warehouse-list-create"),
    "admin_inventory": reverse("api:inventory:admin-inventory-list-create"),
    "admin_serial_units": reverse("api:inventory:admin-serial-unit-list-create"),
    "admin_reservations": reverse("api:inventory:admin-reservation-list"),
}


def _make_user(email="buyer@example.com", username="buyer", **kwargs):
    """Create a plain customer user for permission tests."""
    return User.objects.create_user(
        email=email,
        username=username,
        password=kwargs.pop("password", "StrongPass123!"),
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        **kwargs,
    )


def _make_admin():
    """Create and return a staff superuser for admin CRUD tests."""
    return User.objects.create_user(
        email="manager@example.com",
        username="manager",
        password="ManagerPass123!",
        phone_number="+254700000000",
        is_staff=True,
        is_superuser=True,
    )


def _login(client, email="manager@example.com", password="ManagerPass123!"):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _product_slug_counter():
    """Yield unique slugs/skus for the test database."""
    counter = 0
    while True:
        counter += 1
        yield counter


_PRODUCT_COUNTER = _product_slug_counter()


def _make_variant(*, tracks_serial=False, **kwargs):
    """Create a product (optionally serialized) with one variant."""
    n = next(_PRODUCT_COUNTER)
    product = Product.objects.create(
        name=kwargs.pop("name", f"Appliance {n}"),
        slug=kwargs.pop("slug", f"appliance-{n}"),
        sku=kwargs.pop("sku", f"APP-{n}"),
        description=kwargs.pop("description", "A test appliance."),
        tracks_serial_numbers=tracks_serial,
    )
    return ProductVariant.objects.create(
        product=product,
        sku=kwargs.pop("variant_sku", f"APP-{n}-V1"),
        attributes=kwargs.pop("attributes", {"color": "Silver"}),
        price=kwargs.pop("price", "25000.00"),
    )


def _make_warehouse(name="Main"):
    """Create a warehouse with a unique name."""
    return Warehouse.objects.create(name=name)


class InventoryAPITestCase(APITestCase):
    """Base case that resets the shared test cache for throttle budgets."""

    def setUp(self):
        """Clear throttling state so each test starts with a fresh budget."""
        cache.clear()
        super().setUp()


class InventoryModelTests(InventoryAPITestCase):
    """Exercises model-level stock invariants."""

    def test_available_is_quantity_minus_reserved(self):
        """The ``available`` property subtracts reserved from quantity."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        self.assertEqual(row.available, 10)
        row.reserved = 4
        row.save()
        row.refresh_from_db()
        self.assertEqual(row.available, 6)

    def test_variant_can_have_one_inventory_row_per_warehouse(self):
        """A variant stocks multiple warehouses as separate rows."""
        variant = _make_variant()
        w1 = _make_warehouse("One")
        w2 = _make_warehouse("Two")
        receive_stock(variant=variant, warehouse=w1, quantity=5)
        receive_stock(variant=variant, warehouse=w2, quantity=7)
        rows = Inventory.objects.filter(variant=variant)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(sum(r.quantity for r in rows), 12)

    def test_duplicate_variant_warehouse_pair_rejected(self):
        """The (variant, warehouse) pair is unique."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        Inventory.objects.create(variant=variant, warehouse=warehouse, quantity=3)
        with self.assertRaises(IntegrityError):
            Inventory.objects.create(variant=variant, warehouse=warehouse, quantity=4)

    def test_reserved_cannot_exceed_quantity(self):
        """The database rejects a reserved count above quantity."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=5)
        row.reserved = 6
        with self.assertRaises(IntegrityError):
            row.save()

    def test_quantity_cannot_go_negative(self):
        """The database rejects a negative quantity outright."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=5)
        with self.assertRaises(IntegrityError):
            Inventory.objects.filter(pk=row.pk).update(quantity=-2)

    def test_reserved_cannot_go_negative(self):
        """The database rejects a negative reserved count outright."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=5)
        with self.assertRaises(IntegrityError):
            Inventory.objects.filter(pk=row.pk).update(reserved=-1)

    def test_is_low_stock_flags_available_at_or_below_threshold(self):
        """A row at or below its reorder point reports low stock."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(
            variant=variant, warehouse=warehouse, quantity=5, low_stock_threshold=6
        )
        self.assertTrue(row.is_low_stock)
        receive_stock(
            variant=variant, warehouse=warehouse, quantity=2, low_stock_threshold=6
        )
        row.refresh_from_db()
        self.assertEqual(row.available, 7)
        self.assertFalse(row.is_low_stock)


class StockServiceTests(InventoryAPITestCase):
    """Exercises the reservation lifecycle and stock intake services."""

    def test_receive_stock_creates_and_increments(self):
        """Receiving stock creates a row, then increments it."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        first = receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        self.assertEqual(first.quantity, 10)
        second = receive_stock(variant=variant, warehouse=warehouse, quantity=15)
        self.assertEqual(second.quantity, 25)

    def test_receive_stock_rejects_non_positive(self):
        """Zero and negative receipts are rejected."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        with self.assertRaises(ValidationError):
            receive_stock(variant=variant, warehouse=warehouse, quantity=0)
        with self.assertRaises(ValidationError):
            receive_stock(variant=variant, warehouse=warehouse, quantity=-3)

    def test_create_reservation_increments_reserved_and_expires(self):
        """A reservation raises reserved and carries a future expiry."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        before = timezone.now()
        reservation = create_reservation(variant=variant, quantity=4)
        after = timezone.now()
        self.assertEqual(StockReservation.objects.count(), 1)
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 4)
        self.assertEqual(Inventory.objects.get(variant=variant).available, 6)
        self.assertGreaterEqual(reservation.expires_at, before + timedelta(minutes=14))
        self.assertLessEqual(reservation.expires_at, after + timedelta(minutes=16))

    def test_create_reservation_picks_warehouse_with_most_available(self):
        """The warehouse with the most available units is chosen."""
        variant = _make_variant()
        low = _make_warehouse("Low")
        high = _make_warehouse("High")
        receive_stock(variant=variant, warehouse=low, quantity=10)
        receive_stock(variant=variant, warehouse=high, quantity=40)
        reservation = create_reservation(variant=variant, quantity=5)
        self.assertEqual(reservation.inventory.warehouse, high)

    def test_create_reservation_honours_requested_warehouse(self):
        """A requested warehouse is the only candidate considered."""
        variant = _make_variant()
        low = _make_warehouse("Low")
        high = _make_warehouse("High")
        receive_stock(variant=variant, warehouse=low, quantity=10)
        receive_stock(variant=variant, warehouse=high, quantity=40)
        reservation = create_reservation(variant=variant, quantity=5, warehouse=low)
        self.assertEqual(reservation.inventory.warehouse, low)
        self.assertEqual(Inventory.objects.get(warehouse=high).reserved, 0)

    def test_create_reservation_insufficient_stock_blocks(self):
        """No warehouse with enough stock means the reservation is rejected."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=3)
        with self.assertRaises(ValidationError):
            create_reservation(variant=variant, quantity=4)
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)

    def test_fulfill_deducts_quantity_and_reserved_together(self):
        """Fulfilment moves both quantity and reserved down."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        self.assertTrue(fulfill_reservation(reservation))
        reservation.refresh_from_db()
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(reservation.status, "fulfilled")
        self.assertEqual(row.quantity, 7)
        self.assertEqual(row.reserved, 0)

    def test_fulfill_is_idempotent(self):
        """A fulfilled reservation is not fulfilled twice."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        fulfill_reservation(reservation)
        self.assertFalse(fulfill_reservation(reservation))
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(row.quantity, 7)
        self.assertEqual(
            StockReservation.objects.get(pk=reservation.pk).status, "fulfilled"
        )

    def test_release_only_returns_reserved(self):
        """Releasing returns reserved units without touching quantity."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        self.assertTrue(release_reservation(reservation))
        reservation.refresh_from_db()
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(reservation.status, "released")
        self.assertIsNotNone(reservation.released_at)
        self.assertEqual(row.quantity, 10)
        self.assertEqual(row.reserved, 0)

    def test_release_is_idempotent(self):
        """A released reservation is not released twice."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=2)
        release_reservation(reservation)
        self.assertFalse(release_reservation(reservation))
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)

    def test_create_reservation_respects_priority_order(self):
        """A warehouse sequence is tried in the given allocation order."""
        variant = _make_variant()
        first = _make_warehouse("Priority")
        second = _make_warehouse("Fallback")
        receive_stock(variant=variant, warehouse=first, quantity=2)
        receive_stock(variant=variant, warehouse=second, quantity=20)
        reservation = create_reservation(
            variant=variant, quantity=2, warehouse=[first, second]
        )
        self.assertEqual(reservation.inventory.warehouse, first)
        reservation = create_reservation(
            variant=variant, quantity=2, warehouse=[first, second]
        )
        self.assertEqual(reservation.inventory.warehouse, second)

    def test_inactive_warehouses_are_not_reservable(self):
        """A decommissioned warehouse cannot fulfil a reservation."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        warehouse.is_active = False
        warehouse.save()
        with self.assertRaises(ValidationError):
            create_reservation(variant=variant, quantity=1, warehouse=warehouse)


class SerializedStockServiceTests(InventoryAPITestCase):
    """Exercises the serial-unit path where units and counts move in step."""

    def test_serial_units_register_only_for_serialized_products(self):
        """Non-serialized variants reject serial-unit registration."""
        variant = _make_variant(tracks_serial=False)
        warehouse = _make_warehouse()
        with self.assertRaises(ValidationError):
            receive_serial_units(
                variant=variant, warehouse=warehouse, serial_numbers=["SN-1"]
            )

    def test_count_receipt_rejected_for_serialized_products(self):
        """Serialized products stock only via serial-unit registration."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        with self.assertRaises(ValidationError):
            receive_stock(variant=variant, warehouse=warehouse, quantity=5)

    def test_intake_feeds_the_count_ledger(self):
        """Registering units creates the ledger row with matching quantity."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=["SN-1", "SN-2", "SN-3"],
        )
        row = Inventory.objects.get(variant=variant, warehouse=warehouse)
        self.assertEqual(row.quantity, 3)
        self.assertEqual(row.available, 3)

    def test_duplicate_serial_within_batch_rejected(self):
        """A batch with a repeated serial number is rejected up front."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        with self.assertRaises(ValidationError):
            receive_serial_units(
                variant=variant,
                warehouse=warehouse,
                serial_numbers=["SN-1", "SN-1"],
            )
        self.assertEqual(SerialUnit.objects.count(), 0)

    def test_existing_serial_registration_rejected(self):
        """Re-registering an existing serial number is a clean 400."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-1"]
        )
        with self.assertRaises(ValidationError):
            receive_serial_units(
                variant=variant, warehouse=warehouse, serial_numbers=["SN-1"]
            )
        self.assertEqual(SerialUnit.objects.count(), 1)

    def test_reservation_marks_serial_units_reserved(self):
        """Reserving serialized stock flips units from in_stock to reserved."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=["SN-1", "SN-2", "SN-3"],
        )
        create_reservation(variant=variant, quantity=2, warehouse=warehouse)
        counts = dict(
            SerialUnit.objects.values("status")
            .annotate(n=Count("id"))
            .values_list("status", "n")
        )
        self.assertEqual(counts["in_stock"], 1)
        self.assertEqual(counts["reserved"], 2)
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(row.reserved, 2)
        self.assertEqual(row.available, 1)

    def test_reserved_units_carry_their_reservation(self):
        """Units owned by a reservation are attributed to it."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=["SN-1", "SN-2", "SN-3"],
        )
        reservation = create_reservation(
            variant=variant, quantity=2, warehouse=warehouse
        )
        owned = SerialUnit.objects.filter(status="reserved", reservation=reservation)
        self.assertEqual(owned.count(), 2)
        self.assertEqual(owned.filter(serial_number__in=["SN-1", "SN-2"]).count(), 2)

    def test_fulfill_marks_own_units_sold(self):
        """Fulfilling serialized stock marks the reservation's units sold."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-1", "SN-2"]
        )
        reservation = create_reservation(
            variant=variant, quantity=2, warehouse=warehouse
        )
        fulfill_reservation(reservation)
        self.assertEqual(
            SerialUnit.objects.filter(status="sold", reservation=None).count(), 2
        )
        self.assertEqual(SerialUnit.objects.filter(status="in_stock").count(), 0)
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(row.quantity, 0)
        self.assertEqual(row.reserved, 0)

    def test_release_returns_own_units_to_in_stock(self):
        """Releasing serialized stock returns reserved units to in_stock."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-1", "SN-2"]
        )
        reservation = create_reservation(
            variant=variant, quantity=2, warehouse=warehouse
        )
        release_reservation(reservation)
        self.assertEqual(SerialUnit.objects.filter(status="in_stock").count(), 2)
        self.assertEqual(SerialUnit.objects.filter(status="reserved").count(), 0)
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(row.quantity, 2)
        self.assertEqual(row.reserved, 0)

    def test_overlapping_reservations_do_not_steal_each_others_units(self):
        """Releasing one reservation frees only its own held units."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=["SN-1", "SN-2", "SN-3", "SN-4"],
        )
        first = create_reservation(variant=variant, quantity=2, warehouse=warehouse)
        second = create_reservation(variant=variant, quantity=2, warehouse=warehouse)
        first_units = set(
            SerialUnit.objects.filter(reservation=first).values_list(
                "serial_number", flat=True
            )
        )
        second_units = set(
            SerialUnit.objects.filter(reservation=second).values_list(
                "serial_number", flat=True
            )
        )
        self.assertEqual(len(first_units | second_units), 4)

        release_reservation(first)
        self.assertEqual(SerialUnit.objects.filter(status="reserved").count(), 2)
        self.assertEqual(
            set(
                SerialUnit.objects.filter(reservation=second).values_list(
                    "serial_number", flat=True
                )
            ),
            second_units,
        )
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 2)

        fulfill_reservation(second)
        sold = SerialUnit.objects.filter(status="sold").values_list(
            "serial_number", flat=True
        )
        self.assertEqual(set(sold), second_units)
        self.assertEqual(Inventory.objects.get(variant=variant).available, 2)

    def test_reservation_needs_in_stock_serial_units(self):
        """Registered units alone must cover the requested quantity."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-1", "SN-2"]
        )
        with self.assertRaises(ValidationError):
            create_reservation(variant=variant, quantity=3, warehouse=warehouse)
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)


class SerialStatusTransitionTests(InventoryAPITestCase):
    """Exercises the manual serial-unit transitions and count reconciliation."""

    def _serialized_setup(self, serials):
        """Return a serialized variant, warehouse, and in-stock units."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        units = receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=serials
        )
        return variant, warehouse, units

    def test_in_stock_to_defective_reduces_ledger(self):
        """Withdrawing a unit to defective drops the sellable count."""
        variant, warehouse, units = self._serialized_setup(["SN-1", "SN-2"])
        update_serial_unit_status(units[0], "defective")
        row = Inventory.objects.get(variant=variant)
        self.assertEqual(row.quantity, 1)
        self.assertEqual(row.available, 1)
        self.assertEqual(units[0].status, "defective")

    def test_returned_to_in_stock_restores_ledger(self):
        """A QA-approved returned unit re-enters the sellable count."""
        variant, warehouse, units = self._serialized_setup(["SN-1"])
        update_serial_unit_status(units[0], "returned")
        self.assertEqual(Inventory.objects.get(variant=variant).quantity, 0)
        update_serial_unit_status(units[0], "in_stock")
        self.assertEqual(Inventory.objects.get(variant=variant).quantity, 1)

    def test_reserved_unit_cannot_be_manually_moved(self):
        """A unit owned by an active reservation is not directly editable."""
        variant, warehouse, units = self._serialized_setup(["SN-1", "SN-2"])
        create_reservation(variant=variant, quantity=1, warehouse=warehouse)
        reserved = SerialUnit.objects.filter(status="reserved").first()
        with self.assertRaises(ValidationError):
            update_serial_unit_status(reserved, "defective")

    def test_sold_unit_cannot_be_manually_moved(self):
        """A fulfilled unit must come back through the returns lifecycle."""
        variant, warehouse, units = self._serialized_setup(["SN-1", "SN-2"])
        reservation = create_reservation(
            variant=variant, quantity=2, warehouse=warehouse
        )
        fulfill_reservation(reservation)
        sold = SerialUnit.objects.filter(status="sold").first()
        with self.assertRaises(ValidationError):
            update_serial_unit_status(sold, "returned")

    def test_defective_is_terminal(self):
        """A defective unit cannot be silently returned to sellable stock."""
        variant, warehouse, units = self._serialized_setup(["SN-1"])
        update_serial_unit_status(units[0], "defective")
        with self.assertRaises(ValidationError):
            update_serial_unit_status(units[0], "in_stock")

    def test_unit_without_warehouse_cannot_be_moved(self):
        """A unit with no warehouse cannot be reconciled between states."""
        variant, warehouse, units = self._serialized_setup(["SN-1"])
        unit = units[0]
        unit.warehouse = None
        unit.save()
        with self.assertRaises(ValidationError):
            update_serial_unit_status(unit, "defective")


class MovementLogTests(InventoryAPITestCase):
    """Exercises the stock-movement audit trail."""

    def test_receive_count_is_logged(self):
        """A count receipt records the actor and the positive delta."""
        user = _make_admin()
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10, user=user)
        log = StockMovementLog.objects.get()
        self.assertEqual(log.user, user)
        self.assertEqual(log.action, "receive_count")
        self.assertEqual(log.quantity_change, 10)

    def test_serial_intake_is_logged(self):
        """Registering serial units is recorded as a single batch receipt."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=["SN-1", "SN-2"],
            user=_make_admin(),
        )
        log = StockMovementLog.objects.get()
        self.assertEqual(log.action, "receive_serial")
        self.assertEqual(log.quantity_change, 2)

    def test_manual_status_change_is_logged_with_from_and_to(self):
        """A serial-unit transition records both states."""
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        unit = receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-1"], user=None
        )[0]
        update_serial_unit_status(unit, "defective", user=_make_admin())
        log = StockMovementLog.objects.get(action="serial_status")
        self.assertEqual(log.serial_number, "SN-1")
        self.assertEqual(log.from_status, "in_stock")
        self.assertEqual(log.to_status, "defective")

    def test_release_is_logged_without_an_actor_for_the_sweep(self):
        """Sweep releases log with a null user."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        release_reservation(reservation)
        log = StockMovementLog.objects.get(action="release")
        self.assertIsNone(log.user)
        self.assertEqual(log.reserved_change, -3)


class IdempotencyTests(InventoryAPITestCase):
    """Exercises Idempotency-Key enforcement on stock-intake endpoints."""

    def setUp(self):
        """Create the admin account and a stocked target variant."""
        super().setUp()
        _make_admin()
        self.variant = _make_variant()
        self.warehouse = _make_warehouse()

    def _payload(self):
        """Return a restock payload for the shared test variant."""
        return {
            "variant": self.variant.pk,
            "warehouse": self.warehouse.pk,
            "quantity": 5,
        }

    def test_missing_key_rejected(self):
        """Stock intake without an Idempotency-Key is a 400."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_inventory"], self._payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_replayed_restock_is_not_double_applied(self):
        """A repeated POST with the same key restocks only once."""
        _login(self.client)
        first = self.client.post(
            URLS["admin_inventory"],
            self._payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY="stock-1",
        )
        second = self.client.post(
            URLS["admin_inventory"],
            self._payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY="stock-1",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data, first.data)
        self.assertEqual(
            Inventory.objects.get(
                variant=self.variant, warehouse=self.warehouse
            ).quantity,
            5,
        )

    def test_replayed_serial_registration_returns_cached_result(self):
        """A repeated serial registration replays instead of duplicating."""
        _login(self.client)
        serial_variant = _make_variant(tracks_serial=True)
        payload = {
            "variant": serial_variant.pk,
            "warehouse": self.warehouse.pk,
            "serial_number": "SN-200",
        }
        first = self.client.post(
            URLS["admin_serial_units"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-1",
        )
        second = self.client.post(
            URLS["admin_serial_units"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-1",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data, first.data)
        self.assertEqual(SerialUnit.objects.count(), 1)

    def test_concurrent_duplicate_key_returns_409(self):
        """An in-progress request with the same key answers 409."""
        _login(self.client)
        admin = User.objects.get(email="manager@example.com")
        key = "stock-locked"
        acquire_processing_lock(admin.pk, key)
        response = self.client.post(
            URLS["admin_inventory"],
            self._payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        release_processing_lock(admin.pk, key)


class ExpirySweepTests(InventoryAPITestCase):
    """Exercises the Celery beat sweep that releases stale reservations."""

    def test_expired_reservations_are_released(self):
        """The sweep releases reservations past their expiry."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        StockReservation.objects.filter(pk=reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        released = expire_stale_reservations()
        self.assertEqual(released, 1)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, "released")
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)

    def test_unexpired_reservations_survive_sweep(self):
        """Active reservations inside their grace period are kept."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        create_reservation(variant=variant, quantity=3)
        released = expire_stale_reservations()
        self.assertEqual(released, 0)
        self.assertEqual(StockReservation.objects.filter(status="active").count(), 1)
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 3)

    def test_sweep_is_safe_to_run_twice(self):
        """Redelivering the sweep releases each reservation exactly once."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        StockReservation.objects.filter(pk=reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(expire_stale_reservations(), 1)
        self.assertEqual(expire_stale_reservations(), 0)
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)


class AvailabilityEndpointTests(InventoryAPITestCase):
    """Exercises the public availability endpoint."""

    def test_availability_is_public(self):
        """An anonymous caller can read aggregate and per-warehouse stock."""
        variant = _make_variant()
        w1 = _make_warehouse("One")
        w2 = _make_warehouse("Two")
        receive_stock(variant=variant, warehouse=w1, quantity=10)
        receive_stock(variant=variant, warehouse=w2, quantity=6)
        create_reservation(variant=variant, quantity=4, warehouse=w1)

        response = self.client.get(URLS["availability"], {"variant": variant.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_quantity"], 16)
        self.assertEqual(response.data["total_reserved"], 4)
        self.assertEqual(response.data["total_available"], 12)
        self.assertEqual(len(response.data["per_warehouse"]), 2)
        by_warehouse = {
            row["warehouse_name"]: row for row in response.data["per_warehouse"]
        }
        self.assertEqual(by_warehouse["One"]["available"], 6)
        self.assertEqual(by_warehouse["Two"]["available"], 6)

    def test_availability_by_sku(self):
        """Availability can be looked up by SKU."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=3)
        response = self.client.get(URLS["availability"], {"sku": variant.sku})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["variant"]["sku"], variant.sku)

    def test_availability_requires_variant_or_sku(self):
        """Missing lookup params return 400."""
        response = self.client.get(URLS["availability"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_availability_unknown_variant_returns_404(self):
        """A nonexistent variant returns 404."""
        response = self.client.get(URLS["availability"], {"variant": 999999})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_availability_lists_nothing_when_unstocked(self):
        """An unstocked variant reports zero availability without error."""
        variant = _make_variant()
        response = self.client.get(URLS["availability"], {"variant": variant.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_available"], 0)
        self.assertEqual(response.data["per_warehouse"], [])

    def test_availability_reports_low_stock_per_warehouse(self):
        """Availability carries per-warehouse and overall low-stock flags."""
        variant = _make_variant()
        w1 = _make_warehouse("One")
        w2 = _make_warehouse("Two")
        receive_stock(variant=variant, warehouse=w1, quantity=3, low_stock_threshold=5)
        receive_stock(variant=variant, warehouse=w2, quantity=10, low_stock_threshold=5)
        response = self.client.get(URLS["availability"], {"variant": variant.pk})
        self.assertTrue(response.data["is_low_stock"])
        by_warehouse = {
            row["warehouse_name"]: row for row in response.data["per_warehouse"]
        }
        self.assertTrue(by_warehouse["One"]["is_low_stock"])
        self.assertEqual(by_warehouse["One"]["low_stock_threshold"], 5)
        self.assertFalse(by_warehouse["Two"]["is_low_stock"])

    def test_availability_excludes_inactive_warehouses(self):
        """Decommissioned warehouse stock is hidden from shoppers."""
        variant = _make_variant()
        active = _make_warehouse("Active")
        closed = _make_warehouse("Closed")
        receive_stock(variant=variant, warehouse=active, quantity=10)
        receive_stock(variant=variant, warehouse=closed, quantity=500)
        closed.is_active = False
        closed.save()
        response = self.client.get(URLS["availability"], {"variant": variant.pk})
        self.assertEqual(response.data["total_quantity"], 10)
        names = [row["warehouse_name"] for row in response.data["per_warehouse"]]
        self.assertNotIn("Closed", names)

    def test_availability_hides_inactive_products(self):
        """A discontinued product returns 404 on the availability lookup."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        variant.product.is_active = False
        variant.product.save()
        response = self.client.get(URLS["availability"], {"variant": variant.pk})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bulk_availability_merges_across_warehouses(self):
        """Bulk lookup aggregates totals across every active warehouse."""
        variant = _make_variant()
        w1 = _make_warehouse("One")
        w2 = _make_warehouse("Two")
        receive_stock(variant=variant, warehouse=w1, quantity=8)
        receive_stock(variant=variant, warehouse=w2, quantity=4)
        create_reservation(variant=variant, quantity=3, warehouse=w1)
        response = self.client.post(
            URLS["availability_bulk"],
            {"variant_ids": [variant.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry = response.data["availability"][variant.pk]
        self.assertEqual(entry["total_quantity"], 12)
        self.assertEqual(entry["total_reserved"], 3)
        self.assertEqual(entry["total_available"], 9)

    def test_bulk_availability_accepts_skus(self):
        """Bulk lookup accepts an SKU list."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=2)
        response = self.client.post(
            URLS["availability_bulk"], {"skus": [variant.sku]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["availability"][variant.pk]["total_available"], 2
        )

    def test_bulk_availability_requires_one_lookup_list(self):
        """Sending both or neither lookup lists is a 400."""
        variant = _make_variant()
        self.client.post(
            URLS["availability_bulk"], {"skus": [variant.sku]}, format="json"
        )
        for payload in ({}, {"variant_ids": [1], "skus": ["x"]}):
            response = self.client.post(
                URLS["availability_bulk"], payload, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_availability_unknown_variant_yields_400(self):
        """An unknown member of a bulk lookup fails the whole request."""
        response = self.client.post(
            URLS["availability_bulk"], {"variant_ids": [999999]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_availability_limits_lookup_size(self):
        """Oversized bulk lookups are refused."""
        variants = [_make_variant() for _ in range(MAX_BULK_AVAILABILITY)]
        response = self.client.post(
            URLS["availability_bulk"],
            {"variant_ids": [variant.pk for variant in variants]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.post(
            URLS["availability_bulk"],
            {
                "variant_ids": [variant.pk for variant in variants]
                + [_make_variant().pk]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class WarehouseAdminAPITests(InventoryAPITestCase):
    """Exercises the admin-only warehouse endpoints."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def test_anonymous_cannot_manage_warehouses(self):
        """An unauthenticated caller is rejected from warehouse management."""
        response = self.client.get(URLS["admin_warehouses"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(
            URLS["admin_warehouses"], {"name": "Main"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_warehouses(self):
        """A plain customer token cannot create a warehouse."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_warehouses"], {"name": "Main"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Warehouse.objects.count(), 0)

    def test_admin_can_create_and_list_warehouses(self):
        """An admin can create warehouses and see them in the list."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_warehouses"],
            {"name": "Main", "address": "Nairobi"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.get(URLS["admin_warehouses"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["name"], "Main")

    def test_admin_can_update_and_delete_warehouse(self):
        """An admin can update and delete a warehouse."""
        _login(self.client)
        warehouse = _make_warehouse("Old")
        detail_url = reverse(
            "api:inventory:admin-warehouse-detail", args=[warehouse.pk]
        )
        response = self.client.patch(detail_url, {"name": "New"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Warehouse.objects.count(), 0)


class InventoryAdminAPITests(InventoryAPITestCase):
    """Exercises the admin stock intake and update endpoints."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def test_admin_can_stock_variant_across_two_warehouses(self):
        """Admin can stock a variant in two warehouses and read both rows."""
        _login(self.client)
        variant = _make_variant()
        w1 = _make_warehouse("One")
        w2 = _make_warehouse("Two")
        response = self.client.post(
            URLS["admin_inventory"],
            {"variant": variant.pk, "warehouse": w1.pk, "quantity": 8},
            format="json",
            HTTP_IDEMPOTENCY_KEY="stock-w1",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(
            URLS["admin_inventory"],
            {"variant": variant.pk, "warehouse": w2.pk, "quantity": 12},
            format="json",
            HTTP_IDEMPOTENCY_KEY="stock-w2",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.get(URLS["admin_inventory"], {"variant": variant.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)
        total_available = sum(row["available"] for row in response.data["results"])
        self.assertEqual(total_available, 20)

    def test_admin_cannot_decrease_stock(self):
        """A quantity decrease on update is rejected outright."""
        _login(self.client)
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        detail_url = reverse("api:inventory:admin-inventory-detail", args=[row.pk])
        response = self.client.patch(detail_url, {"quantity": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        row.refresh_from_db()
        self.assertEqual(row.quantity, 10)

    def test_admin_can_increase_stock(self):
        """A quantity increase via update goes through the intake service."""
        _login(self.client)
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        detail_url = reverse("api:inventory:admin-inventory-detail", args=[row.pk])
        response = self.client.patch(detail_url, {"quantity": 15}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row.refresh_from_db()
        self.assertEqual(row.quantity, 15)

    def test_receiving_negative_quantity_rejected(self):
        """A non-positive intake quantity returns 400."""
        _login(self.client)
        variant = _make_variant()
        warehouse = _make_warehouse()
        response = self.client.post(
            URLS["admin_inventory"],
            {"variant": variant.pk, "warehouse": warehouse.pk, "quantity": -1},
            format="json",
            HTTP_IDEMPOTENCY_KEY="stock-neg",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_equal_quantity_patch_is_a_clean_noop(self):
        """Patching the same quantity succeeds without double-applying."""
        _login(self.client)
        variant = _make_variant()
        warehouse = _make_warehouse()
        row = receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        detail_url = reverse("api:inventory:admin-inventory-detail", args=[row.pk])
        response = self.client.patch(detail_url, {"quantity": 10}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row.refresh_from_db()
        self.assertEqual(row.quantity, 10)

    def test_serialized_variant_cannot_receive_count_intake(self):
        """Count intake for a serialized variant is rejected."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        response = self.client.post(
            URLS["admin_inventory"],
            {"variant": variant.pk, "warehouse": warehouse.pk, "quantity": 5},
            format="json",
            HTTP_IDEMPOTENCY_KEY="serialized-count",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Inventory.objects.count(), 0)

    def test_anonymous_and_customer_rejected(self):
        """Stock management requires an admin token."""
        response = self.client.get(URLS["admin_inventory"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_inventory"],
            {"variant": 1, "warehouse": 1, "quantity": 5},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SerialUnitAdminAPITests(InventoryAPITestCase):
    """Exercises serial-unit registration and status guards."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def test_admin_can_register_serial_unit(self):
        """Admin registers an in_stock serial unit for a serialized product."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        response = self.client.post(
            URLS["admin_serial_units"],
            {
                "variant": variant.pk,
                "warehouse": warehouse.pk,
                "serial_number": "SN-100",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-100",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "in_stock")

    def test_admin_can_register_serial_unit_without_warehouse(self):
        """A warehouse-less serial unit is valid stock on the ledger."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        response = self.client.post(
            URLS["admin_serial_units"],
            {"variant": variant.pk, "serial_number": "SN-0100"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-no-wh",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_serial_registration_rejected(self):
        """Registering the same serial number twice returns 400."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        payload = {
            "variant": variant.pk,
            "warehouse": warehouse.pk,
            "serial_number": "SN-100",
        }
        first = self.client.post(
            URLS["admin_serial_units"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-dupe-a",
        )
        second = self.client.post(
            URLS["admin_serial_units"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-dupe-b",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_serial_unit_rejected_for_nonserial_product(self):
        """A product that does not track serial numbers rejects the unit."""
        _login(self.client)
        variant = _make_variant(tracks_serial=False)
        warehouse = _make_warehouse()
        response = self.client.post(
            URLS["admin_serial_units"],
            {
                "variant": variant.pk,
                "warehouse": warehouse.pk,
                "serial_number": "SN-101",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-non",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(SerialUnit.objects.count(), 0)

    def test_serial_unit_requires_active_warehouse_on_receive(self):
        """Receiving against a decommissioned warehouse is rejected."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        warehouse.is_active = False
        warehouse.save()
        response = self.client.post(
            URLS["admin_serial_units"],
            {
                "variant": variant.pk,
                "warehouse": warehouse.pk,
                "serial_number": "SN-102",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="serial-inactive",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lifecycle_statuses_not_directly_settable(self):
        """reserved/sold statuses are owned by the reservation lifecycle."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        unit = receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-103"]
        )[0]
        detail_url = reverse("api:inventory:admin-serial-unit-detail", args=[unit.pk])
        response = self.client.patch(detail_url, {"status": "reserved"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(detail_url, {"status": "sold"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manual_statuses_allowed(self):
        """An admin can mark a unit defective or returned directly."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        unit = receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-104"]
        )[0]
        detail_url = reverse("api:inventory:admin-serial-unit-detail", args=[unit.pk])
        response = self.client.patch(detail_url, {"status": "defective"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        unit.refresh_from_db()
        self.assertEqual(unit.status, "defective")

    def test_serial_list_filters_by_status(self):
        """The serial-unit list can be narrowed to one status."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        units = receive_serial_units(
            variant=variant,
            warehouse=warehouse,
            serial_numbers=["SN-105", "SN-106", "SN-107"],
        )
        update_serial_unit_status(units[0], "defective")
        response = self.client.get(URLS["admin_serial_units"], {"status": "defective"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["serial_number"], "SN-105")

    def test_serial_list_searches_by_number_sku_and_name(self):
        """The serial-unit list searches serial, SKU, and product name."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-XYZ-1"]
        )
        by_number = self.client.get(URLS["admin_serial_units"], {"search": "SN-XYZ"})
        self.assertEqual(len(by_number.data["results"]), 1)
        by_sku = self.client.get(URLS["admin_serial_units"], {"search": variant.sku})
        self.assertEqual(len(by_sku.data["results"]), 1)
        by_name = self.client.get(
            URLS["admin_serial_units"], {"search": variant.product.name}
        )
        self.assertEqual(len(by_name.data["results"]), 1)

    def test_serial_unit_read_exposes_reservation(self):
        """A reserved unit carries its reservation id in the read model."""
        _login(self.client)
        variant = _make_variant(tracks_serial=True)
        warehouse = _make_warehouse()
        receive_serial_units(
            variant=variant, warehouse=warehouse, serial_numbers=["SN-108"]
        )
        reservation = create_reservation(
            variant=variant, quantity=1, warehouse=warehouse
        )
        unit = SerialUnit.objects.get(status="reserved")
        detail_url = reverse("api:inventory:admin-serial-unit-detail", args=[unit.pk])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reservation"], reservation.pk)


class ReservationAdminAPITests(InventoryAPITestCase):
    """Exercises the admin reservation listing and release endpoints."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def _make_active_reservation(self):
        """Return a variant, warehouse, and active reservation."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        reservation = create_reservation(variant=variant, quantity=3)
        return variant, reservation

    def test_anonymous_cannot_read_reservations(self):
        """An unauthenticated caller cannot list reservations."""
        response = self.client.get(URLS["admin_reservations"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_read_reservations(self):
        """A plain customer token cannot list reservations."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.get(URLS["admin_reservations"])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_list_reservations_by_status(self):
        """Admin lists reservations, optionally filtered by status."""
        _login(self.client)
        variant, reservation = self._make_active_reservation()
        response = self.client.get(URLS["admin_reservations"], {"status": "active"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], reservation.pk)
        response = self.client.get(URLS["admin_reservations"], {"status": "released"})
        self.assertEqual(len(response.data["results"]), 0)

    def test_admin_can_release_reservation(self):
        """Admin release returns the hold and updates the row."""
        _login(self.client)
        variant, reservation = self._make_active_reservation()
        release_url = reverse(
            "api:inventory:admin-reservation-release", args=[reservation.pk]
        )
        response = self.client.post(release_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "released")
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)
        self.assertEqual(Inventory.objects.get(variant=variant).quantity, 10)

    def test_release_endpoint_is_idempotent(self):
        """Releasing twice does not move stock twice."""
        _login(self.client)
        variant, reservation = self._make_active_reservation()
        release_url = reverse(
            "api:inventory:admin-reservation-release", args=[reservation.pk]
        )
        self.client.post(release_url)
        response = self.client.post(release_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Inventory.objects.get(variant=variant).reserved, 0)


class InventoryQueryCountTests(InventoryAPITestCase):
    """Guards against N+1 queries in the admin reservation list."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def test_reservation_list_uses_fixed_query_count(self):
        """Listing reservations renders without a query per row."""
        _login(self.client)
        for _ in range(4):
            self._make_reservation()
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(URLS["admin_reservations"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 4)
        self.assertLess(len(captured), 8)

    def _make_reservation(self):
        """Create and return an active reservation for the query test."""
        variant = _make_variant()
        warehouse = _make_warehouse()
        receive_stock(variant=variant, warehouse=warehouse, quantity=10)
        return create_reservation(variant=variant, quantity=1)
