"""Tests for the dashboard app.

Covers the security matrix on every widget endpoint — anonymous rejection, a
customer token rejection, and staff-role restriction (analyst and manager in,
support/courier out) — plus per-module reconciliation against hand-seeded
rows: conversion-rate math, COD status splits, reservation-expiry boundaries,
orphaned-discontinued detection, collection refresh staleness, bundle attach
rate and discount cost, promotions expiring-soon boundaries, return
resolution timing, warehouse-routing gaps, support-queue age, and every alert
type firing inside and staying silent just outside its seeded window. Query
parameter validation follows the analytics convention: unknown parameters are
rejected, and the current-state widgets accept none at all.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bundles.models import Bundle
from apps.catalog.models import Product, ProductVariant
from apps.collections.models import Collection
from apps.inventory.models import Inventory, StockReservation, Warehouse
from apps.orders.models import Order, OrderItem, OrderVerification
from apps.promotions.models import Coupon, Discount
from apps.returns.models import ReturnRequest
from apps.shipping.models import DeliveryZone, WarehouseZonePriority
from apps.social_proof.models import ProductViewEvent
from apps.support.models import Ticket

SALES_URL = "api:dashboard:sales"
COD_URL = "api:dashboard:cod-operations"
STOCK_URL = "api:dashboard:stock"
PRODUCTS_URL = "api:dashboard:products"
COLLECTIONS_URL = "api:dashboard:collections"
BUNDLES_URL = "api:dashboard:bundles"
PROMOTIONS_URL = "api:dashboard:promotions"
RETURNS_URL = "api:dashboard:returns"
WAREHOUSE_URL = "api:dashboard:warehouse-routing"
SUPPORT_URL = "api:dashboard:support"
ALERTS_URL = "api:dashboard:alerts"

ENDPOINTS = [
    SALES_URL,
    COD_URL,
    STOCK_URL,
    PRODUCTS_URL,
    COLLECTIONS_URL,
    BUNDLES_URL,
    PROMOTIONS_URL,
    RETURNS_URL,
    WAREHOUSE_URL,
    SUPPORT_URL,
    ALERTS_URL,
]


def _make_user(email="buyer@example.com", username="buyer", role="customer", **kwargs):
    """Create a user holding the given role."""
    return User.objects.create_user(
        email=email,
        username=username,
        password="StrongPass123!",
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        role=role,
        **kwargs,
    )


def _login(client, email="buyer@example.com", password="StrongPass123!"):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_order(user, *, status_name, subtotal, grand_total, placed_at=None, **kwargs):
    """Create an order, honoring a requested ``placed_at``.

    ``Order.placed_at`` is ``auto_now_add``, so a value passed at creation is
    ignored; the bound is assigned via ``update`` on the created row so the
    timestamp can be seeded deterministically.
    """
    order = Order.objects.create(
        user=user,
        phone=kwargs.pop("phone", "+254712345678"),
        status=status_name,
        subtotal=subtotal,
        grand_total=grand_total,
        **kwargs,
    )
    if placed_at is not None:
        Order.objects.filter(pk=order.pk).update(placed_at=placed_at)
    return order


def _make_product(sku, name, **kwargs):
    """Create a catalogue product and its default active variant."""
    product = Product.objects.create(
        name=name,
        slug=sku.lower(),
        sku=sku,
        description="For dashboard tests",
        **kwargs,
    )
    variant = ProductVariant.objects.create(
        product=product, sku=f"{sku}-VAR", price=Decimal("1000.00")
    )
    return product, variant


def _make_inventory(variant, warehouse_name="Nairobi WH", **kwargs):
    """Create a warehouse and an inventory row for a variant."""
    warehouse = Warehouse.objects.create(name=warehouse_name)
    return Inventory.objects.create(
        variant=variant,
        warehouse=warehouse,
        quantity=kwargs.pop("quantity", 10),
        reserved=kwargs.pop("reserved", 0),
        low_stock_threshold=kwargs.pop("low_stock_threshold", 5),
        **kwargs,
    )


def _make_item(order, variant, *, quantity=1, unit_price, total_price, **kwargs):
    """Create a snapshotted order line for a variant."""
    return OrderItem.objects.create(
        order=order,
        product=variant.product,
        variant_sku=variant.sku,
        product_name=variant.product.name,
        unit_price=unit_price,
        quantity=quantity,
        total_price=total_price,
        tax_rate=Decimal("16.00"),
        **kwargs,
    )


def _set_created_at(model_cls, instance, value):
    """Seed the ``auto_now_add`` ``created_at`` of a row via queryset update."""
    model_cls.objects.filter(pk=instance.pk).update(created_at=value)
    return model_cls.objects.get(pk=instance.pk)


class _AnalystClient(APITestCase):
    """Base class that logs in an analyst before each test."""

    def setUp(self):
        """Create and authenticate an analyst."""
        cache.clear()
        _make_user(
            email="analyst@example.com",
            username="analyst",
            role="analyst",
            is_staff=True,
        )
        _login(self.client, email="analyst@example.com")


class DashboardAccessControlTests(_AnalystClient):
    """Security matrix: anonymous, cross-role, and role-based restriction."""

    def test_anonymous_cannot_read_any_endpoint(self):
        """Every widget rejects an unauthenticated caller."""
        self.client.credentials()
        for url_name in ENDPOINTS:
            url = reverse(url_name)
            self.assertIn(
                self.client.get(url).status_code,
                (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
                f"{url_name} allowed anonymous access",
            )

    def test_customer_token_rejected(self):
        """A logged-in customer cannot read any widget."""
        _make_user()
        _login(self.client)
        for url_name in ENDPOINTS:
            url = reverse(url_name)
            self.assertEqual(
                self.client.get(url).status_code,
                status.HTTP_403_FORBIDDEN,
                f"{url_name} allowed customer access",
            )

    def test_support_and_courier_roles_rejected(self):
        """Support and courier roles cannot read revenue widgets."""
        for role in ("support", "courier"):
            _make_user(
                email=f"{role}@example.com",
                username=role,
                role=role,
                is_staff=True,
            )
            _login(self.client, email=f"{role}@example.com")
            for url_name in ENDPOINTS:
                url = reverse(url_name)
                self.assertEqual(
                    self.client.get(url).status_code,
                    status.HTTP_403_FORBIDDEN,
                    f"{url_name} allowed {role} access",
                )

    def test_manager_and_analyst_roles_allowed(self):
        """Manager and analyst tokens can read every widget."""
        for url_name in ENDPOINTS:
            url = reverse(url_name)
            self.assertEqual(
                self.client.get(url).status_code,
                status.HTTP_200_OK,
                f"{url_name} rejected analyst access",
            )
        _make_user(
            email="manager@example.com",
            username="manager",
            role="manager",
            is_staff=True,
        )
        _login(self.client, email="manager@example.com")
        for url_name in ENDPOINTS:
            url = reverse(url_name)
            self.assertEqual(
                self.client.get(url).status_code,
                status.HTTP_200_OK,
                f"{url_name} rejected manager access",
            )


class DashboardParamValidationTests(_AnalystClient):
    """Query-string validation on the period-bound and current-state widgets."""

    def test_unknown_parameter_rejected(self):
        """A widget that receives an undeclared parameter returns a 400."""
        url = reverse(SALES_URL)
        response = self.client.get(url, {"garbage": "1"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_period_parameters_accepted(self):
        """Declared period parameters are accepted on period-bound widgets."""
        url = reverse(BUNDLES_URL)
        response = self.client.get(url, {"from": "2024-01-01", "to": "2024-02-01"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_reversed_period_rejected(self):
        """A period with from after to is a 400."""
        url = reverse(RETURNS_URL)
        response = self.client.get(url, {"from": "2024-02-01", "to": "2024-01-01"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_current_state_widgets_reject_every_parameter(self):
        """Current-state widgets accept no query parameters at all."""
        for url_name in (
            COD_URL,
            STOCK_URL,
            COLLECTIONS_URL,
            WAREHOUSE_URL,
            ALERTS_URL,
        ):
            url = reverse(url_name)
            response = self.client.get(url, {"from": "2024-01-01"})
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                f"{url_name} accepted a period it must reject",
            )

    def test_invalid_limit_rejected(self):
        """A products limit outside 1..100 is a 400."""
        url = reverse(PRODUCTS_URL)
        response = self.client.get(url, {"limit": 999})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_limit_caps_top_products(self):
        """A products limit caps how many top products are returned."""
        for index in range(3):
            product, variant = _make_product(f"SKU{index}", f"Product {index}")
            order = _make_order(
                _make_user(email=f"u{index}@example.com", username=f"u{index}"),
                status_name="delivered",
                subtotal=Decimal("100.00"),
                grand_total=Decimal("100.00"),
            )
            _make_item(
                order,
                variant,
                unit_price=Decimal("100.00"),
                total_price=Decimal("100.00"),
            )
        url = reverse(PRODUCTS_URL)
        response = self.client.get(url, {"limit": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["top"]), 2)


class SalesDashboardTests(_AnalystClient):
    """The sales widget reconciles conversion and period bounds."""

    def test_conversion_rate_reconciles_against_seeded_rows(self):
        """Conversion is delivered orders over recorded views."""
        user = _make_user()
        for _ in range(2):
            _make_order(
                user,
                status_name="delivered",
                subtotal=Decimal("5000.00"),
                grand_total=Decimal("5000.00"),
            )
        product, _ = _make_product("VIEWED", "Viewed Product")
        for index in range(4):
            ProductViewEvent.objects.create(
                product=product, session_key=f"session-{index}"
            )
        response = self.client.get(reverse(SALES_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["summary"]["order_count"], 2)
        self.assertEqual(data["summary"]["order_value"], Decimal("10000.00"))
        self.assertEqual(data["conversion"]["views"], 4)
        self.assertEqual(data["conversion"]["delivered"], 2)
        self.assertEqual(data["conversion"]["delivered_rate_percent"], Decimal("50.00"))

    def test_period_excludes_out_of_range_orders(self):
        """Orders and views outside the period do not count toward conversion."""
        user = _make_user()
        _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("5000.00"),
            grand_total=Decimal("5000.00"),
            placed_at=timezone.now() - timedelta(days=2),
        )
        _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("9000.00"),
            grand_total=Decimal("9000.00"),
            placed_at=timezone.now() - timedelta(days=30),
        )
        product, _ = _make_product("VIEWED2", "Viewed Again")
        ProductViewEvent.objects.create(product=product, session_key="s-1")
        ProductViewEvent.objects.create(product=product, session_key="s-2")
        response = self.client.get(reverse(SALES_URL))
        self.assertEqual(response.data["conversion"]["delivered"], 2)
        self.assertEqual(
            response.data["conversion"]["delivered_rate_percent"], Decimal("100.00")
        )
        now = timezone.now()
        response = self.client.get(
            reverse(SALES_URL),
            {
                "from": (now - timedelta(days=3)).isoformat(),
                "to": now.isoformat(),
            },
        )
        self.assertEqual(response.data["conversion"]["delivered"], 1)
        self.assertEqual(
            response.data["conversion"]["delivered_rate_percent"], Decimal("50.00")
        )


class CodDashboardTests(_AnalystClient):
    """The COD-operations widget reconciles status splits."""

    def test_verification_status_split(self):
        """The verification split matches the seeded OrderVerification rows."""
        user = _make_user()
        expected = {"pending": 2, "verified": 1, "failed": 1, "expired": 1}
        for index, item in enumerate(expected.items()):
            status_name, count = item
            for _ in range(count):
                order = _make_order(
                    user,
                    status_name="pending",
                    subtotal=Decimal("100.00"),
                    grand_total=Decimal("100.00"),
                )
                OrderVerification.objects.create(
                    order=order,
                    otp_code=f"{index:06d}",
                    phone_number="+254712345678",
                    status=status_name,
                )
        response = self.client.get(reverse(COD_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verifications = response.data["verifications"]
        self.assertEqual(verifications["total"], 5)
        for key, count in expected.items():
            self.assertEqual(
                verifications[key], count, f"verification {key} split wrong"
            )

    def test_cod_order_status_split(self):
        """The COD order split matches seeded COD orders."""
        user = _make_user()
        _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("200.00"),
            grand_total=Decimal("200.00"),
            payment_method="cod",
        )
        _make_order(
            user,
            status_name="delivery_failed",
            subtotal=Decimal("300.00"),
            grand_total=Decimal("300.00"),
            payment_method="cod",
        )
        _make_order(
            user,
            status_name="pending",
            subtotal=Decimal("400.00"),
            grand_total=Decimal("400.00"),
            payment_method="cod",
        )
        _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("500.00"),
            grand_total=Decimal("500.00"),
            payment_method="mpesa",
        )
        response = self.client.get(reverse(COD_URL))
        orders = response.data["orders"]
        self.assertEqual(orders["total"], 3)
        self.assertEqual(orders["delivered"], 1)
        self.assertEqual(orders["delivery_failed"], 1)
        self.assertEqual(orders["open"], 1)


class StockDashboardTests(_AnalystClient):
    """The stock widget reconciles snapshot and reservation windows."""

    def test_snapshot_reconciles_across_warehouses(self):
        """Units, reserved, and available are summed across warehouses."""
        first_product, first_variant = _make_product("SNAP1", "Snap One")
        _make_inventory(first_variant, "Nairobi WH", quantity=6, reserved=2)
        _make_inventory(first_variant, "Mombasa WH", quantity=4, reserved=0)
        response = self.client.get(reverse(STOCK_URL))
        snapshot = response.data["snapshot"]
        self.assertEqual(snapshot["units"], 10)
        self.assertEqual(snapshot["reserved"], 2)
        self.assertEqual(snapshot["available"], 8)

    def test_reservation_expiry_boundary(self):
        """Reservations just inside the expiry window are flagged, just outside not."""
        _, variant = _make_product("RES1", "Reserved One")
        inventory = _make_inventory(variant, "Nairobi WH", quantity=10)
        StockReservation.objects.create(
            inventory=inventory,
            quantity=1,
            expires_at=timezone.now() + timedelta(minutes=14),
        )
        StockReservation.objects.create(
            inventory=inventory,
            quantity=1,
            expires_at=timezone.now() + timedelta(minutes=16),
        )
        response = self.client.get(reverse(STOCK_URL))
        reservations = response.data["reservations"]
        self.assertEqual(reservations["active"], 2)
        self.assertEqual(reservations["expiring_soon"], 1)
        self.assertEqual(reservations["overdue_unreleased"], 0)

    def test_overdue_unreleased_boundary(self):
        """Reservations past expiry are overdue; released ones are not counted."""
        _, variant = _make_product("RES2", "Reserved Two")
        inventory = _make_inventory(variant, "Nairobi WH", quantity=10)
        StockReservation.objects.create(
            inventory=inventory,
            quantity=1,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        StockReservation.objects.create(
            inventory=inventory,
            quantity=1,
            expires_at=timezone.now() + timedelta(minutes=1),
            status="released",
        )
        response = self.client.get(reverse(STOCK_URL))
        reservations = response.data["reservations"]
        self.assertEqual(reservations["active"], 1)
        self.assertEqual(reservations["overdue_unreleased"], 1)


class ProductsDashboardTests(_AnalystClient):
    """The products widget reconciles rankings and catalogue health."""

    def test_discontinued_without_replacement_flagged(self):
        """Only discontinued products without a replacement are flagged."""
        _make_product("ORPHAN", "No Successor", is_discontinued=True)
        product_with_replacement, _ = _make_product(
            "REPLACED", "Has Successor", is_discontinued=True
        )
        successor, _ = _make_product("SUCCESSOR", "Successor")
        product_with_replacement.replacement_product = successor
        product_with_replacement.save()
        _make_product("KEEP", "Still For Sale", is_discontinued=False)
        response = self.client.get(reverse(PRODUCTS_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        flagged = response.data["discontinued_without_replacement"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["sku"], "ORPHAN")
        self.assertEqual(response.data["catalogue"]["discontinued"], 2)
        self.assertEqual(
            response.data["catalogue"]["discontinued_without_replacement"], 1
        )

    def test_top_products_ordered_by_revenue(self):
        """The top list orders products by revenue from snapshot order lines."""
        _, first_variant = _make_product("TOP1", "Top One")
        _, second_variant = _make_product("TOP2", "Top Two")
        user = _make_user()
        order = _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("1200.00"),
            grand_total=Decimal("1200.00"),
        )
        _make_item(
            order,
            first_variant,
            quantity=1,
            unit_price=Decimal("800.00"),
            total_price=Decimal("800.00"),
        )
        _make_item(
            order,
            second_variant,
            quantity=3,
            unit_price=Decimal("400.00"),
            total_price=Decimal("1200.00"),
        )
        response = self.client.get(reverse(PRODUCTS_URL))
        top = response.data["top"]
        self.assertEqual(top[0]["variant_sku"], "TOP2-VAR")
        self.assertEqual(top[0]["revenue"], Decimal("1200.00"))
        self.assertEqual(top[1]["variant_sku"], "TOP1-VAR")


class CollectionsDashboardTests(_AnalystClient):
    """The collections widget reflects the refresh status recording."""

    def test_refresh_status_reflects_seeded_last_run(self):
        """Staleness tracks last_refreshed_at against the threshold."""
        now = timezone.now()
        Collection.objects.create(
            name="Recent",
            slug="recent",
            collection_type="smart",
            smart_rule="new_arrivals",
            last_refreshed_at=now - timedelta(minutes=5),
        )
        Collection.objects.create(
            name="Stale",
            slug="stale",
            collection_type="smart",
            smart_rule="new_arrivals",
            last_refreshed_at=now - timedelta(minutes=45),
        )
        Collection.objects.create(
            name="Never",
            slug="never",
            collection_type="smart",
            smart_rule="best_sellers",
        )
        Collection.objects.create(name="Curated", slug="curated")
        response = self.client.get(reverse(COLLECTIONS_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_slug = {entry["slug"]: entry for entry in response.data["collections"]}
        self.assertFalse(by_slug["recent"]["is_stale"])
        self.assertTrue(by_slug["stale"]["is_stale"])
        self.assertTrue(by_slug["never"]["is_stale"])
        self.assertIsNone(by_slug["never"]["refresh_age_minutes"])
        self.assertFalse(by_slug["curated"]["is_stale"])
        self.assertEqual(response.data["stale_count"], 2)
        self.assertEqual(response.data["smart_count"], 3)


class BundlesDashboardTests(_AnalystClient):
    """The bundles widget reconciles attach rate and discount cost."""

    def test_bundle_attach_rate_and_discount_cost(self):
        """Attach rate and discount cost match the seeded bundle purchases."""
        bundle = Bundle.objects.create(
            name="Kitchen Duo",
            slug="kitchen-duo",
            discount_type="percent",
            discount_value=Decimal("10.00"),
        )
        user = _make_user()
        bundled_order = _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("1800.00"),
            grand_total=Decimal("1800.00"),
        )
        _, first_variant = _make_product("BDL1", "Bundle One")
        _, second_variant = _make_product("BDL2", "Bundle Two")
        group = uuid4()
        _make_item(
            bundled_order,
            first_variant,
            quantity=1,
            unit_price=Decimal("1000.00"),
            total_price=Decimal("900.00"),
            bundle=bundle,
            bundle_group_id=group,
        )
        _make_item(
            bundled_order,
            second_variant,
            quantity=1,
            unit_price=Decimal("1000.00"),
            total_price=Decimal("900.00"),
            bundle=bundle,
            bundle_group_id=group,
        )
        for _ in range(3):
            _make_order(
                user,
                status_name="delivered",
                subtotal=Decimal("300.00"),
                grand_total=Decimal("300.00"),
            )
        response = self.client.get(reverse(BUNDLES_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_orders"], 4)
        self.assertEqual(data["bundle_orders"], 1)
        self.assertEqual(data["attach_rate_percent"], Decimal("25.00"))
        self.assertEqual(data["discount_cost"], Decimal("200.00"))
        self.assertEqual(len(data["bundles"]), 1)
        entry = data["bundles"][0]
        self.assertEqual(entry["purchases"], 1)
        self.assertEqual(entry["units"], 2)
        self.assertEqual(entry["revenue"], Decimal("1800.00"))
        self.assertEqual(entry["discount_given"], Decimal("200.00"))


class PromotionsDashboardTests(_AnalystClient):
    """The promotions widget flags campaigns expiring inside the window."""

    def test_expiring_soon_boundary(self):
        """Campaigns ending inside the window are listed; slower ones are not."""
        now = timezone.now()
        Discount.objects.create(
            name="Seventy Two",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=now - timedelta(days=7),
            ends_at=now + timedelta(hours=1),
        )
        Discount.objects.create(
            name="Weeks Away",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=now - timedelta(days=7),
            ends_at=now + timedelta(hours=50),
        )
        Coupon.objects.create(
            code="SOON22",
            discount_type="fixed",
            value=Decimal("200.00"),
            starts_at=now - timedelta(days=2),
            ends_at=now + timedelta(hours=12),
        )
        Coupon.objects.create(
            code="LATER22",
            discount_type="fixed",
            value=Decimal("200.00"),
            starts_at=now - timedelta(days=2),
            ends_at=now + timedelta(hours=96),
        )
        response = self.client.get(reverse(PROMOTIONS_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expiring = response.data["expiring_soon"]
        self.assertEqual(len(expiring), 2)
        labels = {entry["label"] for entry in expiring}
        self.assertEqual(labels, {"Seventy Two", "SOON22"})

    def test_no_expiring_entries_when_none_close(self):
        """An empty expiring-soon list is returned when the window is clear."""
        now = timezone.now()
        Discount.objects.create(
            name="Far Away",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=now - timedelta(days=7),
            ends_at=now + timedelta(hours=96),
        )
        response = self.client.get(reverse(PROMOTIONS_URL))
        self.assertEqual(response.data["expiring_soon"], [])
        self.assertEqual(response.data["expiring_soon_count"], 0)


class ReturnsDashboardTests(_AnalystClient):
    """The returns widget reconciles resolution timing."""

    def test_avg_resolution_time(self):
        """Average resolution hours covers resolved requests in the period."""
        user = _make_user()
        base = timezone.now() - timedelta(days=3)
        fast_order = _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("100.00"),
            grand_total=Decimal("100.00"),
        )
        slow_order = _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("200.00"),
            grand_total=Decimal("200.00"),
        )
        _set_created_at(
            ReturnRequest,
            ReturnRequest.objects.create(
                order=fast_order,
                reason="Damaged",
                status="refunded",
                resolved_at=base + timedelta(hours=2),
            ),
            base,
        )
        _set_created_at(
            ReturnRequest,
            ReturnRequest.objects.create(
                order=slow_order,
                reason="Wrong item",
                status="refunded",
                resolved_at=base + timedelta(hours=6),
            ),
            base,
        )
        response = self.client.get(reverse(RETURNS_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        resolution = response.data["resolution"]
        self.assertEqual(resolution["resolved"], 2)
        self.assertEqual(resolution["avg_resolution_hours"], Decimal("4.00"))

    def test_stuck_open_over_threshold(self):
        """Return requests awaiting action beyond the stuck threshold are counted."""
        user = _make_user()
        order = _make_order(
            user,
            status_name="delivered",
            subtotal=Decimal("100.00"),
            grand_total=Decimal("100.00"),
        )
        _set_created_at(
            ReturnRequest,
            ReturnRequest.objects.create(
                order=order, reason="Stuck", status="requested"
            ),
            timezone.now() - timedelta(hours=73),
        )
        _set_created_at(
            ReturnRequest,
            ReturnRequest.objects.create(
                order=order, reason="Fresh", status="requested"
            ),
            timezone.now() - timedelta(hours=2),
        )
        response = self.client.get(reverse(RETURNS_URL))
        self.assertEqual(response.data["resolution"]["stuck_open_over_threshold"], 1)


class WarehouseRoutingDashboardTests(_AnalystClient):
    """The warehouse-routing widget surfaces zones without a priority."""

    def test_zones_without_priority_surfaced_as_gaps(self):
        """Zones with zero WarehouseZonePriority rows are flagged as gaps."""
        covered = DeliveryZone.objects.create(
            county="Nairobi", area_name="Westlands", base_fee=Decimal("200.00")
        )
        DeliveryZone.objects.create(
            county="Nairobi", area_name="Githurai", base_fee=Decimal("250.00")
        )
        DeliveryZone.objects.create(
            county="Mombasa", area_name="Nyali", base_fee=Decimal("300.00")
        )
        warehouse = Warehouse.objects.create(name="Nairobi WH")
        WarehouseZonePriority.objects.create(
            delivery_zone=covered, warehouse=warehouse, priority=0
        )
        response = self.client.get(reverse(WAREHOUSE_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["zones_total"], 3)
        self.assertEqual(data["zones_covered"], 1)
        self.assertEqual(data["coverage_percent"], Decimal("33.33"))
        self.assertEqual(len(data["gaps"]), 2)
        gap_areas = {(entry["county"], entry["area_name"]) for entry in data["gaps"]}
        self.assertIn(("Nairobi", "Githurai"), gap_areas)
        self.assertIn(("Mombasa", "Nyali"), gap_areas)


class SupportDashboardTests(_AnalystClient):
    """The support widget reconciles the open-queue age breakdown."""

    def test_open_queue_age(self):
        """Oldest and threshold-crossing open tickets match the seeded rows."""
        user = _make_user()
        _set_created_at(
            Ticket,
            Ticket.objects.create(
                user=user,
                category="order_issue",
                subject="Overdue",
                status="open",
            ),
            timezone.now() - timedelta(hours=49),
        )
        _set_created_at(
            Ticket,
            Ticket.objects.create(
                user=user,
                category="complaint",
                subject="Recent",
                status="pending_customer",
            ),
            timezone.now() - timedelta(hours=2),
        )
        Ticket.objects.create(
            user=user,
            category="product_question",
            subject="Resolved",
            status="resolved",
        )
        response = self.client.get(reverse(SUPPORT_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        age = response.data["age"]
        self.assertEqual(age["old_ticket_threshold_hours"], 24)
        self.assertEqual(age["open_older_than_threshold"], 1)
        self.assertEqual(age["oldest_open_age_hours"], Decimal("49.0"))

    def test_by_category_breakdown_present(self):
        """The category breakdown from the composed aggregate is returned."""
        user = _make_user()
        Ticket.objects.create(user=user, category="complaint", subject="C")
        Ticket.objects.create(user=user, category="return", subject="R")
        Ticket.objects.create(user=user, category="return", subject="R2")
        response = self.client.get(reverse(SUPPORT_URL))
        categories = response.data["summary"]["by_category"]
        by_category = {row["category"]: row["count"] for row in categories}
        self.assertEqual(by_category["complaint"], 1)
        self.assertEqual(by_category["return"], 2)


class AlertsDashboardTests(_AnalystClient):
    """Every alert fires under its seeded boundary and stays silent outside."""

    def test_low_stock_alert_boundary(self):
        """Inventory at or below the threshold alerts; above it stays silent."""
        _, low_variant = _make_product("LOW1", "Low Stock")
        _make_inventory(low_variant, "Nairobi WH", quantity=2, reserved=2)
        _, fine_variant = _make_product("FINE1", "Fine Stock")
        _make_inventory(fine_variant, "Mombasa WH", quantity=20)
        response = self.client.get(reverse(ALERTS_URL))
        low_alerts = [a for a in response.data["alerts"] if a["type"] == "low_stock"]
        self.assertEqual(len(low_alerts), 1)
        self.assertEqual(low_alerts[0]["count"], 1)
        self.assertEqual(low_alerts[0]["severity"], "critical")

    def test_low_stock_silent_just_above_threshold(self):
        """Available units just above the reorder point produce no alert."""
        _, variant = _make_product("HIGH1", "High Stock")
        _make_inventory(variant, "Nairobi WH", quantity=6, low_stock_threshold=5)
        response = self.client.get(reverse(ALERTS_URL))
        self.assertFalse(any(a["type"] == "low_stock" for a in response.data["alerts"]))

    def test_reservation_expiry_alert_boundary(self):
        """Active reservations inside the window alert; outside stays silent."""
        _, variant = _make_product("RESA", "Alert Reservation")
        inventory = _make_inventory(variant, "Nairobi WH", quantity=10)
        StockReservation.objects.create(
            inventory=inventory,
            quantity=1,
            expires_at=timezone.now() + timedelta(minutes=14),
        )
        StockReservation.objects.create(
            inventory=inventory,
            quantity=1,
            expires_at=timezone.now() + timedelta(minutes=16),
        )
        response = self.client.get(reverse(ALERTS_URL))
        expiry_alerts = [
            a for a in response.data["alerts"] if a["type"] == "reservation_expiring"
        ]
        self.assertEqual(len(expiry_alerts), 1)
        self.assertEqual(expiry_alerts[0]["count"], 1)

    def test_failed_cod_alert_boundary(self):
        """Failed COD deliveries inside 24h alert; older ones stay silent."""
        user = _make_user()
        _make_order(
            user,
            status_name="delivery_failed",
            subtotal=Decimal("300.00"),
            grand_total=Decimal("300.00"),
            payment_method="cod",
        )
        old = _make_order(
            user,
            status_name="delivery_failed",
            subtotal=Decimal("400.00"),
            grand_total=Decimal("400.00"),
            payment_method="cod",
        )
        Order.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(hours=25)
        )
        response = self.client.get(reverse(ALERTS_URL))
        failed = [
            a for a in response.data["alerts"] if a["type"] == "failed_cod_deliveries"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["count"], 1)

    def test_unconfirmed_order_alert_boundary(self):
        """Pending orders past the age threshold alert; fresher ones stay silent."""
        user = _make_user()
        _make_order(
            user,
            status_name="pending",
            subtotal=Decimal("100.00"),
            grand_total=Decimal("100.00"),
            placed_at=timezone.now() - timedelta(minutes=31),
        )
        _make_order(
            user,
            status_name="pending",
            subtotal=Decimal("200.00"),
            grand_total=Decimal("200.00"),
            placed_at=timezone.now() - timedelta(minutes=29),
        )
        response = self.client.get(reverse(ALERTS_URL))
        unconfirmed = [
            a for a in response.data["alerts"] if a["type"] == "unconfirmed_orders"
        ]
        self.assertEqual(len(unconfirmed), 1)
        self.assertEqual(unconfirmed[0]["count"], 1)

    def test_stale_collection_alert_boundary(self):
        """Smart collections never refreshed or past staleness alert; fresh stay silent."""
        now = timezone.now()
        Collection.objects.create(
            name="Never",
            slug="never",
            collection_type="smart",
            smart_rule="new_arrivals",
        )
        Collection.objects.create(
            name="Stale",
            slug="stale",
            collection_type="smart",
            smart_rule="new_arrivals",
            last_refreshed_at=now - timedelta(minutes=31),
        )
        Collection.objects.create(
            name="Fresh",
            slug="fresh",
            collection_type="smart",
            smart_rule="new_arrivals",
            last_refreshed_at=now - timedelta(minutes=29),
        )
        response = self.client.get(reverse(ALERTS_URL))
        stale = [a for a in response.data["alerts"] if a["type"] == "stale_collections"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["count"], 2)

    def test_empty_feed_when_conditions_clear(self):
        """With no seeded issues the alert feed is empty."""
        response = self.client.get(reverse(ALERTS_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["alerts"], [])


class CollectionsServiceRefreshTests(_AnalystClient):
    """The last-refreshed write lands through the collections refresh service."""

    def test_refresh_sets_last_refreshed_at(self):
        """A successful smart refresh records when it last ran."""
        from apps.collections.services import refresh_smart_collection

        product, _ = _make_product("REFRESHED", "Freshly Refreshed")
        collection = Collection.objects.create(
            name="Smart One",
            slug="smart-one",
            collection_type="smart",
            smart_rule="new_arrivals",
        )
        self.assertTrue(refresh_smart_collection(collection))
        refreshed = Collection.objects.get(pk=collection.pk)
        self.assertIsNotNone(refreshed.last_refreshed_at)
