"""Tests for the shipping app.

Covers shipping-fee calculation (physical vs volumetric weight, the
free-shipping threshold, server-side subtotal and package data the client
cannot game, erroring on variants with no package data), warehouse-selection
routing (priority ranking, fallback, inactive exclusion, bounded query
count), the public delivery-zone and quote endpoints (including the weight
breakdown and configured currency), the admin CRUD endpoints (duplicate
rank/county-area rejection, negative and non-positive money guards, the
47-county whitelist) and their database-level constraints, and the security
separation between anonymous, customer, and admin callers.
"""

from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Product, ProductVariant
from apps.core.models import SiteConfig
from apps.inventory.models import Warehouse
from apps.inventory.services import receive_stock
from apps.shipping.models import DeliveryZone, WarehouseZonePriority
from apps.shipping.services import calculate_shipping_fee, select_fulfillment_warehouse

URLS = {
    "delivery_zones": reverse("api:shipping:delivery-zones"),
    "quote": reverse("api:shipping:quote"),
    "admin_zones": reverse("api:shipping:admin-delivery-zone-list-create"),
    "admin_priorities": reverse("api:shipping:admin-zone-priority-list-create"),
}

_PRODUCT_SEQ = [0]


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


def _make_variant(**kwargs):
    """Create a product with one variant, with unique slugs and skus."""
    _PRODUCT_SEQ[0] += 1
    n = _PRODUCT_SEQ[0]
    product = Product.objects.create(
        name=kwargs.pop("name", f"Appliance {n}"),
        slug=kwargs.pop("slug", f"appliance-{n}"),
        sku=kwargs.pop("sku", f"APP-{n}"),
        description=kwargs.pop("description", "A test appliance."),
    )
    return ProductVariant.objects.create(
        product=product,
        sku=kwargs.pop("variant_sku", f"APP-{n}-V1"),
        attributes=kwargs.pop("attributes", {"color": "Silver"}),
        price=kwargs.pop("price", "25000.00"),
        package_weight=kwargs.pop("package_weight", None),
        package_dimensions=kwargs.pop("package_dimensions", {}),
    )


def _make_zone(**kwargs):
    """Create a delivery zone with sensible defaults."""
    return DeliveryZone.objects.create(
        county=kwargs.pop("county", "Nairobi"),
        area_name=kwargs.pop("area_name", "Westlands"),
        base_fee=kwargs.pop("base_fee", "200.00"),
        per_kg_rate=kwargs.pop("per_kg_rate", "50.00"),
        free_shipping_threshold=kwargs.pop("free_shipping_threshold", None),
        estimated_days=kwargs.pop("estimated_days", 2),
        is_active=kwargs.pop("is_active", True),
    )


def _make_warehouse(name):
    """Create a warehouse with a unique name."""
    return Warehouse.objects.create(name=name)


class _Line:
    """Minimal carrier matching the shape the shipping service consumes."""

    def __init__(self, variant, quantity):
        """Store the variant and quantity for this quote line."""
        self.variant = variant
        self.quantity = quantity


class ShippingAPITestCase(APITestCase):
    """Base case that resets the shared test cache for throttle budgets."""

    def setUp(self):
        """Clear throttling state so each test starts with a fresh budget."""
        cache.clear()
        super().setUp()


class ShippingFeeServiceTests(ShippingAPITestCase):
    """Exercises the shipping-fee calculation."""

    def test_fee_is_base_plus_per_kg_on_physical_weight(self):
        """The fee adds the per-kg rate times billable weight to the base."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        variant = _make_variant(package_weight="2.00")
        fee = calculate_shipping_fee(zone, [_Line(variant, 1)])
        self.assertEqual(fee, Decimal("300.00"))
        fee = calculate_shipping_fee(zone, [_Line(variant, 3)])
        self.assertEqual(fee, Decimal("500.00"))

    def test_fee_uses_volumetric_weight_when_larger(self):
        """A light but bulky item is charged by its volumetric weight."""
        zone = _make_zone(base_fee="0.00", per_kg_rate="50.00")
        variant = _make_variant(
            package_weight="2.00",
            package_dimensions={"length": 50, "width": 40, "height": 40},
        )
        # 50 * 40 * 40 / 5000 = 16 kg, which beats the 2 kg physical weight.
        fee = calculate_shipping_fee(zone, [_Line(variant, 1)])
        self.assertEqual(fee, Decimal("800.00"))

    def test_fee_uses_physical_weight_when_larger(self):
        """A heavy item is charged by its physical weight, not volume."""
        zone = _make_zone(base_fee="0.00", per_kg_rate="50.00")
        variant = _make_variant(
            package_weight="20.00",
            package_dimensions={"length": 10, "width": 10, "height": 10},
        )
        # 10 * 10 * 10 / 5000 = 0.2 kg, which loses to the 20 kg physical weight.
        fee = calculate_shipping_fee(zone, [_Line(variant, 1)])
        self.assertEqual(fee, Decimal("1000.00"))

    def test_mixed_weight_cart_aggregates_billable_weight(self):
        """Multiple lines contribute their weighted totals to one fee."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        light = _make_variant(package_weight="2.00")
        heavy = _make_variant(package_weight="10.00")
        fee = calculate_shipping_fee(zone, [_Line(light, 2), _Line(heavy, 2)])
        # (2*2 + 10*2) * 50 + 200 = (4 + 20) * 50 + 200 = 1400
        self.assertEqual(fee, Decimal("1400.00"))

    def test_free_shipping_threshold_waives_fee(self):
        """A subtotal at or above the threshold drops the fee to zero."""
        zone = _make_zone(
            base_fee="200.00", per_kg_rate="50.00", free_shipping_threshold="30000.00"
        )
        variant = _make_variant(package_weight="5.00", price="28000.00")
        lines = [_Line(variant, 1)]
        # Subtotal 28000 < 30000 -> charged.
        self.assertEqual(calculate_shipping_fee(zone, lines), Decimal("450.00"))
        # Subtotal 56000 >= 30000 -> free.
        self.assertEqual(
            calculate_shipping_fee(zone, lines + [_Line(variant, 1)]), Decimal("0.00")
        )

    def test_subtotal_is_recomputed_server_side_not_trusted(self):
        """A client-injected subtotal cannot waive the fee by inflating it."""
        zone = _make_zone(
            base_fee="200.00", per_kg_rate="50.00", free_shipping_threshold="30000.00"
        )
        variant = _make_variant(package_weight="5.00", price="28000.00")
        payload = {
            "delivery_zone": zone.pk,
            "subtotal": "1000000.00",
            "items": [{"variant": variant.pk, "quantity": 1}],
        }
        response = self.client.post(URLS["quote"], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 1 * 28000 < 30000, so the genuine subtotal does not qualify and the
        # fee is still charged despite the inflated client field.
        self.assertEqual(response.data["fee"], "450.00")

    def test_fee_uses_configured_volumetric_divisor(self):
        """The volumetric-weight divisor reads from site settings."""
        from apps.core.models import SiteConfig

        zone = _make_zone(base_fee="0.00", per_kg_rate="50.00")
        variant = _make_variant(
            package_weight="0.00",
            package_dimensions={"length": 100, "width": 100, "height": 100},
        )
        site = SiteConfig.load()
        site.settings = {**site.settings, "volumetric_weight_divisor": 2500}
        site.save()
        # 100*100*100 / 2500 = 400 kg -> fee 20000.
        fee = calculate_shipping_fee(zone, [_Line(variant, 1)])
        self.assertEqual(fee, Decimal("20000.00"))

    def test_volumetric_weight_prices_a_variant_with_no_physical_weight(self):
        """A variant with dimensions but no package weight prices by volume."""
        zone = _make_zone(base_fee="0.00", per_kg_rate="50.00")
        variant = _make_variant(
            package_weight=None,
            package_dimensions={"length": 50, "width": 40, "height": 40},
        )
        # 50 * 40 * 40 / 5000 = 16 kg.
        fee = calculate_shipping_fee(zone, [_Line(variant, 1)])
        self.assertEqual(fee, Decimal("800.00"))

    def test_fee_raises_when_variant_has_no_package_data(self):
        """A variant with neither weight nor dimensions does not price as zero."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        variant = _make_variant(package_weight=None, package_dimensions={})
        with self.assertRaisesRegex(ValueError, "no package weight or dimensions"):
            calculate_shipping_fee(zone, [_Line(variant, 1)])


class WarehouseSelectionServiceTests(ShippingAPITestCase):
    """Exercises the fulfillment-warehouse routing rule."""

    def _stocked(self, variant, warehouse, quantity):
        """Stock a variant in a warehouse and return the warehouse."""
        receive_stock(variant=variant, warehouse=warehouse, quantity=quantity)
        return warehouse

    def test_priority_ranking_is_respected(self):
        """The lowest-priority warehouse with stock wins."""
        variant = _make_variant()
        zone = _make_zone()
        primary = _make_warehouse("Primary")
        fallback = _make_warehouse("Fallback")
        self._stocked(variant, primary, 10)
        self._stocked(variant, fallback, 99)
        WarehouseZonePriority.objects.create(
            delivery_zone=zone, warehouse=primary, priority=0
        )
        WarehouseZonePriority.objects.create(
            delivery_zone=zone, warehouse=fallback, priority=1
        )
        self.assertEqual(select_fulfillment_warehouse(variant, zone, 5), primary)

    def test_skips_priority_warehouse_without_enough_stock(self):
        """A ranked warehouse that is short of stock is skipped."""
        variant = _make_variant()
        zone = _make_zone()
        primary = _make_warehouse("Primary")
        fallback = _make_warehouse("Fallback")
        self._stocked(variant, primary, 2)
        self._stocked(variant, fallback, 20)
        WarehouseZonePriority.objects.create(
            delivery_zone=zone, warehouse=primary, priority=0
        )
        WarehouseZonePriority.objects.create(
            delivery_zone=zone, warehouse=fallback, priority=1
        )
        self.assertEqual(select_fulfillment_warehouse(variant, zone, 5), fallback)

    def test_falls_back_when_no_priority_covers_order(self):
        """A zone with no configured priorities falls back to any warehouse."""
        variant = _make_variant()
        zone = _make_zone()
        other = _make_warehouse("Other")
        self._stocked(variant, other, 8)
        self.assertEqual(select_fulfillment_warehouse(variant, zone, 5), other)

    def test_returns_none_when_no_warehouse_covers_order(self):
        """Insufficient stock everywhere returns None, not a wrong pick."""
        variant = _make_variant()
        zone = _make_zone()
        warehouse = _make_warehouse("Main")
        self._stocked(variant, warehouse, 3)
        self.assertIsNone(select_fulfillment_warehouse(variant, zone, 5))

    def test_inactive_warehouse_is_never_selected(self):
        """A decommissioned warehouse cannot be routed to despite the rank."""
        variant = _make_variant()
        zone = _make_zone()
        closed = _make_warehouse("Closed")
        open_wh = _make_warehouse("Open")
        self._stocked(variant, closed, 50)
        closed.is_active = False
        closed.save()
        self._stocked(variant, open_wh, 1)
        WarehouseZonePriority.objects.create(
            delivery_zone=zone, warehouse=closed, priority=0
        )
        WarehouseZonePriority.objects.create(
            delivery_zone=zone, warehouse=open_wh, priority=1
        )
        self.assertEqual(select_fulfillment_warehouse(variant, zone, 1), open_wh)

    def test_inactive_warehouse_excluded_from_fallback(self):
        """Fallback skips inactive warehouses even with large stock."""
        variant = _make_variant()
        zone = _make_zone()
        closed = _make_warehouse("Closed")
        self._stocked(variant, closed, 999)
        closed.is_active = False
        closed.save()
        self.assertIsNone(select_fulfillment_warehouse(variant, zone, 5))

    def test_fallback_prefers_warehouse_with_most_stock(self):
        """Unranked warehouses are picked by largest available stock, stably."""
        variant = _make_variant()
        zone = _make_zone()
        thin = _make_warehouse("Thin")
        rich = _make_warehouse("Rich")
        self._stocked(variant, thin, 6)
        self._stocked(variant, rich, 20)
        self.assertEqual(select_fulfillment_warehouse(variant, zone, 5), rich)

    def test_warehouse_selection_uses_bounded_query_count(self):
        """Routing costs two queries however many warehouses are ranked."""
        variant = _make_variant()
        zone = _make_zone()
        for index, name in enumerate(["Alpha", "Bravo", "Charlie"]):
            warehouse = _make_warehouse(name)
            self._stocked(variant, warehouse, 10)
            WarehouseZonePriority.objects.create(
                delivery_zone=zone, warehouse=warehouse, priority=index
            )
        with CaptureQueriesContext(connection) as captured:
            selected = select_fulfillment_warehouse(variant, zone, 5)
        self.assertEqual(selected.name, "Alpha")
        self.assertLess(len(captured), 5)


class DeliveryZoneEndpointTests(ShippingAPITestCase):
    """Exercises the public delivery-zone catalogue endpoint."""

    def test_delivery_zones_are_public(self):
        """An anonymous caller can list active delivery zones."""
        _make_zone()
        response = self.client.get(URLS["delivery_zones"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_inactive_zones_excluded_from_public_list(self):
        """Inactive zones do not appear in the public catalogue."""
        _make_zone(area_name="Active", is_active=True)
        _make_zone(area_name="Hidden", is_active=False)
        response = self.client.get(URLS["delivery_zones"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["area_name"], "Active")

    def test_county_filter_narrows_the_list(self):
        """A county query filter returns only that county's zones."""
        _make_zone(county="Nairobi", area_name="Westlands")
        _make_zone(county="Mombasa", area_name="Nyali")
        response = self.client.get(URLS["delivery_zones"], {"county": "Nairobi"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["county"], "Nairobi")

    def test_delivery_zone_list_uses_fixed_query_count(self):
        """Listing delivery zones renders without a query per row."""
        for index in range(4):
            _make_zone(area_name=f"Area {index}")
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(URLS["delivery_zones"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 4)
        self.assertLess(len(captured), 8)


class ShippingQuoteEndpointTests(ShippingAPITestCase):
    """Exercises the public shipping-quote endpoint."""

    def _payload(self, zone, lines):
        """Return a quote request payload for a zone and line specs."""
        return {
            "delivery_zone": zone.pk,
            "items": [
                {"variant": variant.pk, "quantity": quantity}
                for variant, quantity in lines
            ],
        }

    def test_quote_is_public_and_returns_fee(self):
        """An anonymous caller can quote a shipping fee."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        variant = _make_variant(package_weight="2.00")
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["fee"], "300.00")
        self.assertFalse(response.data["free_shipping"])
        self.assertEqual(response.data["currency"], "KES")

    def test_quote_reports_free_shipping(self):
        """A subtotal beyond the threshold flags free shipping."""
        zone = _make_zone(
            base_fee="200.00",
            per_kg_rate="50.00",
            free_shipping_threshold="30000.00",
        )
        variant = _make_variant(package_weight="2.00", price="40000.00")
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["fee"], "0.00")
        self.assertTrue(response.data["free_shipping"])

    def test_quote_aggregates_mixed_weight_lines(self):
        """Mixed-weight carts are quoted across all lines."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        light = _make_variant(package_weight="2.00")
        heavy = _make_variant(package_weight="10.00")
        response = self.client.post(
            URLS["quote"],
            self._payload(zone, [(light, 2), (heavy, 2)]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["fee"], "1400.00")

    def test_quote_rejects_inactive_zone(self):
        """An inactive zone cannot be quoted against."""
        zone = _make_zone(is_active=False)
        variant = _make_variant(package_weight="2.00")
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quote_rejects_discontinued_product_variant(self):
        """A discontinued product is not quotable."""
        zone = _make_zone()
        variant = _make_variant(package_weight="2.00")
        variant.product.is_discontinued = True
        variant.product.save()
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quote_rejects_inactive_product_variant(self):
        """An inactive product is not quotable."""
        zone = _make_zone()
        variant = _make_variant(package_weight="2.00")
        variant.product.is_active = False
        variant.product.save()
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quote_requires_items(self):
        """A quote with no lines is rejected."""
        zone = _make_zone()
        response = self.client.post(
            URLS["quote"], {"delivery_zone": zone.pk, "items": []}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quote_rejects_unknown_variant(self):
        """A nonexistent variant makes the quote a 400."""
        zone = _make_zone()
        response = self.client.post(
            URLS["quote"],
            {"delivery_zone": zone.pk, "items": [{"variant": 999999, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quote_rejects_non_positive_quantity(self):
        """A zero-cart quantity is rejected."""
        zone = _make_zone()
        variant = _make_variant(package_weight="2.00")
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 0)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quote_returns_weight_breakdown_and_estimate(self):
        """The quote reports total and per-line weights plus delivery days."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00", estimated_days=3)
        variant = _make_variant(package_weight="2.00")
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 2)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["estimated_days"], 3)
        # 2 kg * 2 units = 4 kg total, priced at 200 + 50 * 4.
        self.assertEqual(response.data["total_weight_kg"], "4.00")
        self.assertEqual(response.data["lines"][0]["weight_kg"], "4.00")
        self.assertEqual(response.data["fee"], "400.00")

    def test_quote_uses_configured_currency(self):
        """The quoted currency comes from site settings, not a constant."""
        site = SiteConfig.load()
        site.settings = {**site.settings, "currency": "USD"}
        site.save()
        zone = _make_zone()
        variant = _make_variant(package_weight="2.00")
        try:
            response = self.client.post(
                URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
            )
        finally:
            site.settings = {**site.settings, "currency": "KES"}
            site.save()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["currency"], "USD")

    def test_quote_rejects_variant_without_package_data(self):
        """A variant with no package weight or dimensions is a 400, not free."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        variant = _make_variant(package_weight=None, package_dimensions={})
        response = self.client.post(
            URLS["quote"], self._payload(zone, [(variant, 1)]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no package weight or dimensions", str(response.data))

    def test_quote_ignores_client_supplied_package_metrics(self):
        """The fee uses the server's package data, never a client's values."""
        zone = _make_zone(base_fee="200.00", per_kg_rate="50.00")
        variant = _make_variant(package_weight="50.00", price="28000.00")
        payload = {
            "delivery_zone": zone.pk,
            "items": [
                {
                    "variant": variant.pk,
                    "quantity": 1,
                    "package_weight": "0.00",
                    "package_dimensions": {"length": 1, "width": 1, "height": 1},
                }
            ],
        }
        response = self.client.post(URLS["quote"], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 50 kg from the variant record, not the 0 kg a malicious client sent:
        # 200 + 50 * 50 = 2700.
        self.assertEqual(response.data["fee"], "2700.00")


class DeliveryZoneAdminAPITests(ShippingAPITestCase):
    """Exercises the admin-only delivery-zone endpoints."""

    def setUp(self):
        """Create the admin account used by the admin-credential tests."""
        super().setUp()
        _make_admin()

    def test_anonymous_cannot_manage_zones(self):
        """An unauthenticated caller is rejected from zone management."""
        response = self.client.get(URLS["admin_zones"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Nairobi", "area_name": "CBD", "base_fee": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_zones(self):
        """A plain customer token cannot create a delivery zone."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Nairobi", "area_name": "CBD", "base_fee": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(DeliveryZone.objects.count(), 0)

    def test_admin_can_create_and_list_zones(self):
        """An admin can create a zone and see it in the list."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_zones"],
            {
                "county": "Nairobi",
                "area_name": "CBD",
                "base_fee": "200.00",
                "per_kg_rate": "50.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["county"], "Nairobi")
        response = self.client.get(URLS["admin_zones"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_admin_can_update_and_delete_zone(self):
        """An admin can edit and delete a delivery zone."""
        _login(self.client)
        zone = _make_zone(area_name="Old")
        detail_url = reverse("api:shipping:admin-delivery-zone-detail", args=[zone.pk])
        response = self.client.patch(detail_url, {"area_name": "New"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["area_name"], "New")
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(DeliveryZone.objects.count(), 0)

    def test_admin_list_shows_inactive_zones(self):
        """The admin list includes inactive zones for management."""
        _login(self.client)
        _make_zone(area_name="Hidden", is_active=False)
        response = self.client.get(URLS["admin_zones"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_admin_zone_creation_rejects_unlisted_county(self):
        """The county must be one of Kenya's 47 counties."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Atlantis", "area_name": "CBD", "base_fee": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DeliveryZone.objects.count(), 0)

    def test_admin_zone_creation_accepts_alternative_county_spellings(self):
        """Apostrophes, hyphens, and the ``City`` suffix still validate."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Murang'a", "area_name": "Town", "base_fee": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Nairobi City", "area_name": "CBD", "base_fee": "250.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DeliveryZone.objects.count(), 2)

    def test_admin_zone_creation_rejects_duplicate_county_area(self):
        """A county + area may only name one zone."""
        _login(self.client)
        _make_zone(county="Nairobi", area_name="Westlands")
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Nairobi", "area_name": "Westlands", "base_fee": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DeliveryZone.objects.count(), 1)

    def test_admin_zone_creation_rejects_negative_base_fee(self):
        """A negative base fee is rejected at the API."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Nairobi", "area_name": "CBD", "base_fee": "-200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_zone_creation_rejects_negative_per_kg_rate(self):
        """A negative per-kg rate is rejected at the API."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_zones"],
            {"county": "Nairobi", "area_name": "CBD", "per_kg_rate": "-5.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_zone_creation_rejects_non_positive_threshold(self):
        """A zero or negative free-shipping threshold is rejected."""
        _login(self.client)
        for threshold in ("0.00", "-1.00"):
            response = self.client.post(
                URLS["admin_zones"],
                {
                    "county": "Nairobi",
                    "area_name": "CBD",
                    "free_shipping_threshold": threshold,
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zone_db_constraints_reject_negative_fee(self):
        """The database independently rejects negative zone money fields."""
        with self.assertRaises(IntegrityError):
            DeliveryZone.objects.create(
                county="Nairobi", area_name="Lavington", base_fee="-5.00"
            )

    def test_zone_db_constraint_rejects_duplicate_county_area(self):
        """The database enforces one zone per county + area."""
        _make_zone(county="Nairobi", area_name="Westlands")
        with self.assertRaises(IntegrityError):
            DeliveryZone.objects.create(
                county="Nairobi", area_name="Westlands", base_fee="100.00"
            )


class ZonePriorityAdminAPITests(ShippingAPITestCase):
    """Exercises the admin-only zone-priority endpoints."""

    def setUp(self):
        """Create the admin account and a zone used by the permission tests."""
        super().setUp()
        _make_admin()
        self.zone = _make_zone()

    def _priority_payload(self):
        """Return a priority payload referencing a fresh warehouse."""
        return {
            "delivery_zone": self.zone.pk,
            "warehouse": Warehouse.objects.create(name="Priority WH").pk,
            "priority": 0,
        }

    def test_anonymous_cannot_manage_priorities(self):
        """An unauthenticated caller is rejected from priority management."""
        response = self.client.get(URLS["admin_priorities"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(
            URLS["admin_priorities"], self._priority_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_priorities(self):
        """A plain customer token cannot create a routing priority."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
        response = self.client.post(
            URLS["admin_priorities"], self._priority_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(WarehouseZonePriority.objects.count(), 0)

    def test_admin_can_create_and_list_priorities(self):
        """An admin can define routing and read it back."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_priorities"], self._priority_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.get(
            URLS["admin_priorities"], {"delivery_zone": self.zone.pk}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["priority"], 0)
        self.assertEqual(response.data["results"][0]["warehouse_name"], "Priority WH")
        self.assertEqual(response.data["results"][0]["zone_label"], str(self.zone))

    def test_admin_can_update_and_delete_priority(self):
        """An admin can re-rank and remove a routing priority."""
        _login(self.client)
        priority = WarehouseZonePriority.objects.create(
            delivery_zone=self.zone,
            warehouse=Warehouse.objects.create(name="WH"),
            priority=0,
        )
        detail_url = reverse(
            "api:shipping:admin-zone-priority-detail", args=[priority.pk]
        )
        response = self.client.patch(detail_url, {"priority": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(WarehouseZonePriority.objects.count(), 0)

    def test_duplicate_priority_for_zone_is_rejected(self):
        """Two warehouses cannot hold the same rank for one zone."""
        _login(self.client)
        WarehouseZonePriority.objects.create(
            delivery_zone=self.zone,
            warehouse=Warehouse.objects.create(name="WH One"),
            priority=0,
        )
        payload = self._priority_payload()
        payload["warehouse"] = Warehouse.objects.create(name="WH Two").pk
        response = self.client.post(URLS["admin_priorities"], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(WarehouseZonePriority.objects.count(), 1)

    def test_duplicate_warehouse_for_zone_is_rejected(self):
        """The same warehouse cannot be ranked twice for one zone."""
        _login(self.client)
        warehouse = Warehouse.objects.create(name="WH One")
        WarehouseZonePriority.objects.create(
            delivery_zone=self.zone, warehouse=warehouse, priority=0
        )
        payload = self._priority_payload()
        payload["warehouse"] = warehouse.pk
        payload["priority"] = 1
        response = self.client.post(URLS["admin_priorities"], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(WarehouseZonePriority.objects.count(), 1)
