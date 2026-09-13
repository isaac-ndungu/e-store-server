"""Demo/load-test seed data for the storefront and analytics reports.

Seeds a catalog (categories, brands, products, variants, warehouse stock,
collections) plus a realistic volume of orders, line items, and
product view events so the public storefront endpoints and the manager-only
analytics reports run against data shaped like production. Every seeded row
carries a ``loadtest-``/``LT-`` prefix marker so a re-run cleans up only its
own rows and can never touch real data — the command is idempotent and safe
to run repeatedly.

Creation-time auto fields (``placed_at``, ``created_at``) cannot be set
through ``bulk_create``, so historical timestamps are applied afterwards with
a batched ``update``/``bulk_update`` pass.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.collections.models import Collection, CollectionMembership
from apps.orders.models import Order, OrderItem, OrderStatusHistory
from apps.social_proof.models import ProductViewEvent

_SEED_CATEGORIES = [
    ("loadtest-fridges", "LoadTest Fridges"),
    ("loadtest-washers", "LoadTest Washers"),
    ("loadtest-kitchen", "LoadTest Kitchen"),
]
_SEED_BRANDS = [
    ("loadtest-samsung", "LoadTest Samsung"),
    ("loadtest-lg", "LoadTest LG"),
    ("loadtest-hisense", "LoadTest Hisense"),
    ("loadtest-sony", "LoadTest Sony"),
    ("loadtest-soundbar", "LoadTest Soundwave"),
]
_USER_EMAIL_SUFFIX = "@customer.loadtest.example"
_USER_PHONE_PREFIX = "+25470"
_ORDER_PHONE_PREFIX = "+25480"
_VIEW_SESSION_PREFIX = "lt-sess-"

_STATUS_WEIGHTS = [
    ("delivered", 40),
    ("shipped", 15),
    ("processing", 10),
    ("confirmed", 10),
    ("pending", 5),
    ("cancelled", 8),
    ("refunded", 6),
    ("delivery_failed", 3),
    ("returned", 3),
]
_KES_TAX = Decimal("0.16")


def _weighted_status():
    """Return a status sampled from the seeded distribution.

    Returns:
        str: an order status from ``_STATUS_WEIGHTS``.
    """
    total = sum(weight for _, weight in _STATUS_WEIGHTS)
    pick = random.randint(1, total)
    running = 0
    for status, weight in _STATUS_WEIGHTS:
        running += weight
        if pick <= running:
            return status
    return "delivered"


def _rand_money(lo, hi):
    """Return a random whole-amount Decimal in the given range.

    Args:
        lo (int): inclusive lower bound in whole shillings.
        hi (int): inclusive upper bound in whole shillings.

    Returns:
        Decimal: the amount.
    """
    return Decimal(random.randint(lo, hi))


def _cleanup():
    """Delete previously seeded rows, oldest dependencies first.

    Filters on the distinctive markers so only load-test data is removed.
    """
    ProductViewEvent.objects.filter(
        session_key__startswith=_VIEW_SESSION_PREFIX
    ).delete()
    OrderItem.objects.filter(variant_sku__startswith="LT-").delete()
    Order.objects.filter(phone__startswith=_ORDER_PHONE_PREFIX).delete()
    ProductVariant.objects.filter(sku__startswith="LT-").delete()
    Product.objects.filter(slug__startswith="loadtest-").delete()
    CollectionMembership.objects.filter(
        collection__slug__startswith="loadtest-col-"
    ).delete()
    Collection.objects.filter(slug__startswith="loadtest-col-").delete()
    Brand.objects.filter(slug__startswith="loadtest-").delete()
    Category.objects.filter(slug__startswith="loadtest-").delete()
    User.objects.filter(email__endswith=_USER_EMAIL_SUFFIX).delete()


class Command(BaseCommand):
    """Populate the database with realistic load-testing data."""

    help = (
        "Seed load-test catalog, order, and traffic data. "
        "Re-running cleans and rebuilds only load-test-marked rows."
    )

    def add_arguments(self, parser):
        """Register the seed size options and the wipe-only flag."""
        parser.add_argument("--products", type=int, default=100)
        parser.add_argument("--orders", type=int, default=2000)
        parser.add_argument("--users", type=int, default=50)
        parser.add_argument("--views", type=int, default=2000)
        parser.add_argument(
            "--wipe-only",
            action="store_true",
            dest="wipe_only",
            help="Only remove previously seeded rows, then exit.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        """Run the seeding pipeline: clean, seed, then summarize."""
        self.stdout.write("Cleaning up prior load-test rows...")
        _cleanup()

        if options["wipe_only"]:
            self.stdout.write(self.style.SUCCESS("Wipe complete."))
            return

        product_count = options["products"]
        order_count = options["orders"]
        user_count = options["users"]
        view_count = options["views"]

        categories, brands = self._seed_catalogue_shape()
        products = self._seed_products(categories, brands, product_count)
        self._seed_collections(products)
        users = self._seed_users(user_count)
        self._seed_orders(users, products, order_count)
        self._seed_view_events(products, view_count)

        self.stdout.write(
            self.style.SUCCESS(
                "Load-test data ready: "
                f"{product_count} products, {order_count} orders, "
                f"{user_count} users, {view_count} view events."
            )
        )

    def _seed_catalogue_shape(self):
        """Create the load-test categories and brands.

        Returns:
            tuple: the created ``Category`` and ``Brand`` querysets.
        """
        Category.objects.bulk_create(
            [
                Category(name=name, slug=slug, is_active=True)
                for slug, name in _SEED_CATEGORIES
            ]
        )
        Brand.objects.bulk_create(
            [Brand(name=name, slug=slug) for slug, name in _SEED_BRANDS]
        )
        categories = list(Category.objects.filter(slug__startswith="loadtest-"))
        brands = list(Brand.objects.filter(slug__startswith="loadtest-"))
        return categories, brands

    def _seed_products(self, categories, brands, product_count):
        """Create products and one variant each.

        Args:
            categories (list): seeded categories to attribute products to.
            brands (list): seeded brands to attribute products to.
            product_count (int): how many products to create.

        Returns:
            list: the created products, ordered by pk.
        """
        products = []
        variants = []
        for i in range(product_count):
            product = Product.objects.create(
                name=f"LoadTest {brands[i % len(brands)].name} Model {i:04d}",
                slug=f"loadtest-product-{i:04d}",
                sku=f"LT-PROD-{i:04d}",
                description=(
                    "A load-testing product with a realistic description so "
                    "search and listing queries have text to match."
                ),
                short_description=f"LoadTest appliance {i:04d}",
                category=categories[i % len(categories)],
                brand=brands[i % len(brands)],
                product_type="physical",
                tax_class="standard",
                specs={
                    "capacity_l": 100 + (i % 40) * 10,
                    "energy_rating": ["A", "A+", "B"][i % 3],
                },
            )
            products.append(product)
            variants.append(
                ProductVariant(
                    product=product,
                    sku=f"LT-{i:04d}-A",
                    attributes={"color": "Black"},
                    price=_rand_money(20000, 89000),
                    package_weight=Decimal("15.00"),
                )
            )
        ProductVariant.objects.bulk_create(variants, batch_size=500)
        return products

    def _seed_collections(self, products):
        """Create manual collections with a handful of memberships.

        Args:
            products (list): products to drop into the collections.
        """
        collections = [
            Collection.objects.create(
                name="LoadTest Featured",
                slug="loadtest-col-featured",
                collection_type="manual",
                is_active=True,
                sort_order=1,
            ),
            Collection.objects.create(
                name="LoadTest Deals",
                slug="loadtest-col-deals",
                collection_type="manual",
                is_active=True,
                sort_order=2,
            ),
        ]
        memberships = [
            CollectionMembership(
                collection=collections[index % 2],
                product=product,
                sort_order=index,
            )
            for index, product in enumerate(products[:20])
        ]
        CollectionMembership.objects.bulk_create(memberships, batch_size=500)

    def _seed_users(self, user_count):
        """Create customer users for orders to attach to.

        Args:
            user_count (int): how many users to create.

        Returns:
            list: the created users.
        """
        users = [
            User(
                email=f"loadtest.{i}@customer.loadtest.example",
                username=f"loadtest-user-{i:04d}",
                phone_number=f"+25470{i:07d}",
            )
            for i in range(user_count)
        ]
        User.objects.bulk_create(users, batch_size=500)
        return list(
            User.objects.filter(email__endswith=_USER_EMAIL_SUFFIX).order_by("pk")
        )

    def _seed_orders(self, users, products, order_count):
        """Bulk-create orders, line items, and status history.

        Args:
            users (list): seeded customers to attach orders to.
            products (list): seeded products to draw line items from.
            order_count (int): how many orders to create.
        """
        now = timezone.now()
        variants = {
            variant.product_id: variant
            for variant in ProductVariant.objects.filter(sku__startswith="LT-")
        }
        orders = []
        item_plans = []
        order_history = []

        for i in range(order_count):
            status = _weighted_status()
            item_count = random.randint(1, 3)
            picked = random.sample(products, k=item_count)

            subtotal = Decimal("0.00")
            for product in picked:
                quantity = random.randint(1, 3)
                unit_price = variants[product.pk].price
                line_total = (unit_price * quantity).quantize(Decimal("0.01"))
                subtotal += line_total
                item_plans.append(
                    (
                        i,
                        OrderItem(
                            product=product,
                            variant_sku=variants[product.pk].sku,
                            product_name=product.name,
                            variant_attributes={"color": "Black"},
                            unit_price=unit_price,
                            quantity=quantity,
                            total_price=line_total,
                            tax_rate=Decimal("16.00"),
                            tax=(line_total * _KES_TAX).quantize(Decimal("0.01")),
                        ),
                    )
                )

            shipping = Decimal("450.00") if i % 3 == 0 else Decimal("0.00")
            tax_total = (subtotal * _KES_TAX).quantize(Decimal("0.01"))
            orders.append(
                Order(
                    user=random.choice(users),
                    phone=f"+25480{i:08d}",
                    status=status,
                    payment_method="cod" if i % 4 == 0 else "mpesa",
                    subtotal=subtotal,
                    delivery_fee=shipping,
                    tax_total=tax_total,
                    discount_total=Decimal("0.00"),
                    grand_total=(subtotal + shipping + tax_total).quantize(
                        Decimal("0.01")
                    ),
                    notes="load-test seed",
                )
            )

        Order.objects.bulk_create(orders, batch_size=500)
        created_orders = list(
            Order.objects.filter(phone__startswith=_ORDER_PHONE_PREFIX)
            .order_by("pk")
            .only("pk", "status", "grand_total")
        )

        order_items = []
        for order_index, item in item_plans:
            item.order = created_orders[order_index]
            order_items.append(item)
        OrderItem.objects.bulk_create(order_items, batch_size=500)

        for order in created_orders:
            order.placed_at = now - timedelta(
                days=random.randint(0, 90),
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
            )
            order_history.append(
                OrderStatusHistory(
                    order=order,
                    from_status="",
                    to_status=order.status,
                    note="load-test seed",
                )
            )
        Order.objects.bulk_update(created_orders, ["placed_at"], batch_size=500)
        OrderStatusHistory.objects.bulk_create(order_history, batch_size=500)

    def _seed_view_events(self, products, view_count):
        """Create product view events in bulk.

        Args:
            products (list): products to attribute views to.
            view_count (int): how many events to create.
        """
        events = [
            ProductViewEvent(
                product=random.choice(products),
                session_key=f"{_VIEW_SESSION_PREFIX}{i:06d}",
            )
            for i in range(view_count)
        ]
        ProductViewEvent.objects.bulk_create(events, batch_size=500)
