"""Tests for the analytics app.

Covers the security matrix on every report endpoint — anonymous rejection, a
customer token rejection, and staff-role restriction (analyst and manager in,
support/courier out) — plus the reconciliation contract that makes the feature
worth shipping: each summary figure is computed from the source-of-truth tables
and must match hand-seeded rows exactly. Also covers period bounds, time-series
grouping, the ``limit`` cap, and strict rejection of unknown query parameters.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.models import Inventory, StockReservation, Warehouse
from apps.notifications.models import NotificationLog
from apps.orders.models import Order, OrderItem
from apps.payments.models import MpesaB2CPayout, Payment
from apps.promotions.models import Coupon, CouponRedemption, Discount
from apps.returns.models import ReturnRequest
from apps.reviews.models import Review
from apps.social_proof.models import ProductViewEvent
from apps.support.models import Ticket

SUMMARY_URL = "api:analytics:summary"
SALES_URL = "api:analytics:sales-report"
STOCK_URL = "api:analytics:stock-report"
PRODUCTS_URL = "api:analytics:products-report"
PROMOTIONS_URL = "api:analytics:promotions-report"
RETURNS_URL = "api:analytics:returns-report"
SUPPORT_URL = "api:analytics:support-report"
REVIEWS_URL = "api:analytics:reviews-report"
TRAFFIC_URL = "api:analytics:traffic-report"
NOTIFICATIONS_URL = "api:analytics:notifications-report"

ENDPOINTS = [
    SUMMARY_URL,
    SALES_URL,
    STOCK_URL,
    PRODUCTS_URL,
    PROMOTIONS_URL,
    RETURNS_URL,
    SUPPORT_URL,
    REVIEWS_URL,
    TRAFFIC_URL,
    NOTIFICATIONS_URL,
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
    ignored. On an existing row that automatic timestamp no longer fires, so
    the bound is assigned via ``update`` (which also leaves ``updated_at``
    alone) rather than ``save``.

    Returns:
        Order: the created order.
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


class AnalyticsAccessControlTests(_AnalystClient):
    """Security matrix: anonymous, cross-role, and role-based restriction."""

    def test_anonymous_cannot_read_any_endpoint(self):
        """Every report rejects an unauthenticated caller."""
        self.client.credentials()
        for url_name in ENDPOINTS:
            url = reverse(url_name)
            self.assertIn(
                self.client.get(url).status_code,
                (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
                f"{url_name} allowed anonymous access",
            )

    def test_customer_token_rejected(self):
        """A logged-in customer cannot read any report."""
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
        """Support and courier roles cannot read revenue reports."""
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
        """Manager and analyst tokens can read every report."""
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


class SummaryReconciliationTests(_AnalystClient):
    """The dashboard summary must reconcile exactly against seeded rows."""

    def setUp(self):
        """Seed one of every source row the summary reads."""
        super().setUp()
        now = timezone.now()

        self.buyer = _make_user()
        second_buyer = _make_user(email="second@example.com", username="second")

        category = Category.objects.create(name="Fridges", slug="fridges")
        self.product = Product.objects.create(
            name="Smart Fridge",
            slug="smart-fridge",
            sku="FRIDGE-1",
            category=category,
            description="Quiet fridge",
            review_count=1,
            average_rating=Decimal("4.00"),
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            sku="FRIDGE-1-SILVER",
            price=Decimal("30000.00"),
        )
        warehouse = Warehouse.objects.create(name="Main", is_active=True)
        self.inventory = Inventory.objects.create(
            variant=self.variant,
            warehouse=warehouse,
            quantity=10,
            reserved=2,
            low_stock_threshold=5,
        )

        # Two delivered orders plus one cancelled order.
        self.delivered = _make_order(
            self.buyer,
            status_name="delivered",
            subtotal=Decimal("60000.00"),
            grand_total=Decimal("69180.00"),
            payment_method="mpesa",
            shipping_total=Decimal("500.00"),
            shipping_tax_amount=Decimal("80.00"),
            tax_total=Decimal("9600.00"),
            discount_total=Decimal("1000.00"),
            placed_at=now - timedelta(days=2),
        )
        self.other_delivered = _make_order(
            second_buyer,
            status_name="delivered",
            subtotal=Decimal("30000.00"),
            grand_total=Decimal("30000.00"),
            payment_method="cod",
            placed_at=now - timedelta(days=1),
        )
        _make_order(
            second_buyer,
            status_name="cancelled",
            subtotal=Decimal("50000.00"),
            grand_total=Decimal("50000.00"),
            payment_method="mpesa",
            placed_at=now - timedelta(hours=6),
        )

        for order, line_total in (
            (self.delivered, Decimal("60000.00")),
            (self.other_delivered, Decimal("30000.00")),
        ):
            OrderItem.objects.create(
                order=order,
                product=self.product,
                variant_sku=self.variant.sku,
                product_name=self.product.name,
                unit_price=line_total,
                quantity=1,
                total_price=line_total,
                tax_rate=Decimal("16.00"),
                tax=line_total * Decimal("0.16"),
            )

        # Completed payments for the delivered orders plus a failed one.
        Payment.objects.create(
            order=self.delivered,
            provider="mpesa",
            amount=Decimal("69180.00"),
            status="completed",
            created_at=self.delivered.placed_at,
        )
        Payment.objects.create(
            order=self.other_delivered,
            provider="cod",
            amount=Decimal("30000.00"),
            status="completed",
            created_at=self.other_delivered.placed_at,
        )
        Payment.objects.create(
            order=self.delivered,
            provider="mpesa",
            amount=Decimal("69180.00"),
            status="failed",
            created_at=self.delivered.placed_at,
        )

        # A successful refund payout plus an initiation-only one.
        MpesaB2CPayout.objects.create(
            order=self.delivered,
            reason="return_refund",
            phone_number="+254712345678",
            amount=Decimal("5000.00"),
            conversation_id="conv-1",
            status="success",
            created_at=self.delivered.placed_at,
        )
        MpesaB2CPayout.objects.create(
            order=self.delivered,
            reason="return_refund",
            phone_number="+254712345678",
            amount=Decimal("5000.00"),
            conversation_id="conv-2",
            status="pending",
            created_at=self.delivered.placed_at,
        )

        # One active reservation against the seeded stock.
        StockReservation.objects.create(
            inventory=self.inventory,
            quantity=2,
            expires_at=now + timedelta(minutes=20),
            status="active",
        )

        # A redeemed coupon and a live discount.
        self.coupon = Coupon.objects.create(
            code="SAVE10",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=now - timedelta(days=7),
        )
        self.delivered.coupon = self.coupon
        self.delivered.save(update_fields=["coupon"])
        CouponRedemption.objects.create(
            coupon=self.coupon, user=self.buyer, order=self.delivered
        )
        Discount.objects.create(
            name="Launch",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("5.00"),
            starts_at=now - timedelta(days=7),
        )

        # Returns, tickets, reviews, view events, notifications.
        ReturnRequest.objects.create(
            order=self.delivered,
            order_item=self.delivered.items.first(),
            reason="Box damaged",
            status="refunded",
            requested_resolution="refund",
            refund_amount=Decimal("5000.00"),
            refund_method="mpesa_b2c",
            created_at=self.delivered.placed_at,
        )
        Ticket.objects.create(
            user=self.buyer,
            category="order_issue",
            subject="Delayed",
            status="open",
        )
        Ticket.objects.create(
            user=self.buyer,
            category="other",
            subject="Resolved one",
            status="resolved",
        )
        Review.objects.create(
            product=self.product,
            user=self.buyer,
            order_item=self.delivered.items.first(),
            rating=4,
            title="Great",
            body="Works well",
            is_approved=True,
        )
        Review.objects.create(
            product=self.product,
            user=second_buyer,
            rating=3,
            title="Meh",
            body="Okay",
            is_approved=False,
        )
        ProductViewEvent.objects.create(
            product=self.product,
            session_key="sess-1",
            created_at=now - timedelta(days=3),
        )
        NotificationLog.objects.create(
            channel="sms",
            purpose="otp",
            recipient="+254712345678",
            message="Code 123456",
            status="sent",
        )
        NotificationLog.objects.create(
            channel="sms",
            purpose="otp",
            recipient="+254712345678",
            message="Code 654321",
            status="failed",
        )

        self.now = now

    def test_summary_reconciles_against_seeded_rows(self):
        """Every driver KPI matches the hand-seeded source rows."""
        response = self.client.get(reverse(SUMMARY_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        sales = data["sales"]
        # Two kept orders totaling 99,180 gross; discounted by 1,000.
        self.assertEqual(sales["order_count"], 2)
        self.assertEqual(sales["order_value"], Decimal("99180.00"))
        self.assertEqual(sales["avg_order_value"], Decimal("49590.00"))
        self.assertEqual(sales["shipping"], Decimal("500.00"))
        self.assertEqual(sales["discount"], Decimal("1000.00"))
        # Tax: 9600 order VAT + 80 shipping VAT from the first order only.
        self.assertEqual(sales["tax_collected"], Decimal("9680.00"))
        # Collected: two completed payments (69,180 + 30,000).
        self.assertEqual(sales["collected"], Decimal("99180.00"))
        # Refund outflow: the single success payout.
        self.assertEqual(sales["refund_outflow"], Decimal("5000.00"))
        self.assertEqual(sales["net"], Decimal("94180.00"))

        orders_by_status = {o["status"]: o for o in data["orders_by_status"]}
        self.assertEqual(orders_by_status["cancelled"]["count"], 1)
        self.assertEqual(orders_by_status["cancelled"]["value"], Decimal("50000.00"))
        self.assertEqual(orders_by_status["delivered"]["count"], 2)
        self.assertEqual(orders_by_status["delivered"]["value"], Decimal("99180.00"))

        stock = data["stock"]
        self.assertEqual(stock["units"], 10)
        # The active reservation of 2 is what drives Inventory.reserved=2;
        # the snapshot reads the row counts, never a secondary computation.
        self.assertEqual(stock["reserved"], 2)
        self.assertEqual(stock["available"], 8)
        self.assertEqual(stock["low_stock"], 0)
        self.assertEqual(stock["active_reservations"], 1)
        self.assertEqual(stock["variants"], 1)
        self.assertEqual(stock["warehouses"], 1)

        self.assertEqual(data["catalogue"]["products"], 1)
        self.assertEqual(data["catalogue"]["variants"], 1)

        traffic = data["traffic"]
        self.assertEqual(traffic["view_count"], 1)

        promotions = data["promotions"]
        self.assertEqual(promotions["active_discounts"], 1)
        self.assertEqual(promotions["coupon_redemptions"], 1)
        self.assertEqual(promotions["coupon_savings"], Decimal("1000.00"))

        returns = data["returns"]
        self.assertEqual(returns["requested"], 1)
        self.assertEqual(returns["refunded"], 1)
        self.assertEqual(returns["refunded_amount"], Decimal("5000.00"))

        support = data["support"]
        self.assertEqual(support["opened"], 2)
        self.assertEqual(support["resolved"], 1)
        self.assertEqual(support["open_now"], 1)

        reviews = data["reviews"]
        self.assertEqual(reviews["count"], 1)  # only approved
        self.assertEqual(reviews["avg_rating"], Decimal("4.00"))
        self.assertEqual(reviews["total_products"], 1)

        notifications = data["notifications"]
        self.assertEqual(notifications["count"], 2)
        self.assertEqual(notifications["failed"], 1)

        products = data["products"]
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["units"], 2)
        self.assertEqual(products[0]["revenue"], Decimal("90000.00"))
        self.assertEqual(products[0]["order_count"], 2)


class PeriodFilterTests(_AnalystClient):
    """The from/to bounds must restrict every report to the period."""

    def setUp(self):
        """Create two orders: one inside the window, one outside it."""
        super().setUp()
        now = timezone.now()
        inside = now - timedelta(days=3)
        outside = now - timedelta(days=30)

        self.inside_order = _make_order(
            _make_user(),
            status_name="delivered",
            subtotal=Decimal("10000.00"),
            grand_total=Decimal("10000.00"),
            placed_at=inside,
        )
        Payment.objects.create(
            order=self.inside_order,
            provider="cod",
            amount=Decimal("10000.00"),
            status="completed",
            created_at=inside,
        )
        _make_order(
            _make_user(email="old@example.com", username="old"),
            status_name="delivered",
            subtotal=Decimal("99999.00"),
            grand_total=Decimal("99999.00"),
            placed_at=outside,
        )
        self.period_from = (inside - timedelta(days=1)).isoformat()
        self.period_to = (inside + timedelta(days=1)).isoformat()

    def test_sales_period_excludes_out_of_range_rows(self):
        """The sales report only counts orders inside from/to."""
        url = reverse(SALES_URL)
        response = self.client.get(
            url, {"from": self.period_from, "to": self.period_to}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["order_count"], 1)
        self.assertEqual(response.data["summary"]["order_value"], Decimal("10000.00"))

    def test_default_period_is_all_time(self):
        """Without bounds, every order in the dataset counts."""
        response = self.client.get(reverse(SUMMARY_URL))
        sales = response.data["sales"]
        self.assertEqual(sales["order_count"], 2)
        self.assertEqual(sales["order_value"], Decimal("109999.00"))

    def test_reversed_period_rejected(self):
        """A period with from after to is a 400."""
        url = reverse(SALES_URL)
        response = self.client.get(
            url, {"from": self.period_to, "to": self.period_from}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_date_rejected(self):
        """A non-ISO from value is a 400."""
        url = f"{reverse(SALES_URL)}?from=not-a-date"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SalesTimeseriesTests(_AnalystClient):
    """The group_by dimension buckets sales correctly."""

    def test_day_grouping(self):
        """Orders placed on different days land in separate day buckets."""
        now = timezone.now()
        for days_ago, amount in ((2, Decimal("100.00")), (1, Decimal("200.00"))):
            _make_order(
                _make_user(
                    email=f"buyer-{days_ago}@example.com", username=f"b{days_ago}"
                ),
                status_name="delivered",
                subtotal=amount,
                grand_total=amount,
                placed_at=now - timedelta(days=days_ago),
            )
        response = self.client.get(reverse(SALES_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        series = response.data["series"]
        self.assertEqual(len(series), 2)
        buckets = sorted(b["value"] for b in series)
        self.assertEqual(buckets, [Decimal("100.00"), Decimal("200.00")])

    def test_invalid_group_by_rejected(self):
        """An unsupported group_by value is a 400."""
        url = f"{reverse(SALES_URL)}?group_by=hour"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProductPerformanceTests(_AnalystClient):
    """The product report's limit cap and revenue accuracy."""

    def setUp(self):
        """Create two products with sold quantities."""
        super().setUp()
        now = timezone.now()
        buyer = _make_user()
        self.first = Product.objects.create(
            name="First", slug="first", sku="SKU-1", description="d"
        )
        self.second = Product.objects.create(
            name="Second", slug="second", sku="SKU-2", description="d"
        )
        variant_first = ProductVariant.objects.create(
            product=self.first, sku="SKU-1-A", price=Decimal("500.00")
        )
        variant_second = ProductVariant.objects.create(
            product=self.second, sku="SKU-2-A", price=Decimal("1000.00")
        )
        order = Order.objects.create(
            user=buyer,
            phone="+254700000001",
            status="delivered",
            subtotal=Decimal("2500.00"),
            grand_total=Decimal("2500.00"),
            placed_at=now,
        )
        OrderItem.objects.create(
            order=order,
            product=self.first,
            variant_sku=variant_first.sku,
            product_name="First",
            unit_price=Decimal("500.00"),
            quantity=3,
            total_price=Decimal("1500.00"),
            tax_rate=Decimal("16.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.second,
            variant_sku=variant_second.sku,
            product_name="Second",
            unit_price=Decimal("1000.00"),
            quantity=1,
            total_price=Decimal("1000.00"),
            tax_rate=Decimal("16.00"),
        )

    def test_top_product_is_highest_revenue(self):
        """The report ranks products by revenue."""
        response = self.client.get(reverse(PRODUCTS_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        products = response.data["products"]
        self.assertEqual(products[0]["name"], "First")
        self.assertEqual(products[0]["units"], 3)
        self.assertEqual(products[0]["revenue"], Decimal("1500.00"))
        self.assertEqual(products[0]["order_count"], 1)
        self.assertEqual(len(products), 2)

    def test_limit_caps_results(self):
        """The limit parameter caps the number of products returned."""
        url = f"{reverse(PRODUCTS_URL)}?limit=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["products"]), 1)

    def test_invalid_limit_rejected(self):
        """A non-numeric or out-of-range limit is a 400."""
        for bad in ("abc", "0", "101"):
            url = f"{reverse(PRODUCTS_URL)}?limit={bad}"
            self.assertEqual(
                self.client.get(url).status_code, status.HTTP_400_BAD_REQUEST
            )


class QueryWhitelistTests(_AnalystClient):
    """Unknown query parameters are rejected, not silently ignored."""

    def test_unknown_parameter_rejected(self):
        """A report that receives an undeclared parameter returns a 400."""
        url = reverse(SUMMARY_URL)
        response = self.client.get(url, {"garbage": "1"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_known_parameters_accepted(self):
        """The declared parameters are accepted across reports."""
        url = reverse(SALES_URL)
        response = self.client.get(
            url, {"from": "2024-01-01", "to": "2024-02-01", "group_by": "week"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class StockSnapshotTests(_AnalystClient):
    """Stock aggregates span all warehouses, per domain rule one."""

    def test_stock_snapshot_combines_warehouses(self):
        """A variant held across two warehouses contributes both rows."""
        product = Product.objects.create(name="P", slug="p", sku="P-1", description="d")
        variant = ProductVariant.objects.create(
            product=product, sku="P-1-A", price=Decimal("100.00")
        )
        w1 = Warehouse.objects.create(name="W1", is_active=True)
        w2 = Warehouse.objects.create(name="W2", is_active=True)
        Inventory.objects.create(variant=variant, warehouse=w1, quantity=5, reserved=1)
        Inventory.objects.create(variant=variant, warehouse=w2, quantity=3, reserved=0)

        response = self.client.get(reverse(STOCK_URL))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        stock = response.data
        self.assertEqual(stock["units"], 8)
        self.assertEqual(stock["reserved"], 1)
        self.assertEqual(stock["available"], 7)
        self.assertEqual(stock["variants"], 1)
        self.assertEqual(stock["warehouses"], 2)
        # w1 holds 4 available vs its threshold of 5, so it flags low stock;
        # w2's 3 available vs threshold 5 also flags. Both count.
        self.assertEqual(stock["low_stock"], 2)
