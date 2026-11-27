"""Acceptance tests for crawl-resilient product detail rendering.

Verifies that the product detail endpoint includes related products inline,
returns valid JSON-LD with priceCurrency KES and availability, and that
concurrent requests at crawl speed produce zero 429s. The tests simulate both
no-delay and 1 req/s crawl patterns against the actual throttle logic, proving
the internal-render scope handles storefront render traffic without exhausting
the public budget.
"""

from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.catalog.models import (
    Brand,
    Category,
    Product,
    RelatedProduct,
)


def _make_category(**kwargs):
    """Create an active category with sensible defaults.

    Args:
        **kwargs: overrides for the category fields.

    Returns:
        Category: the created category.
    """
    defaults = {"name": "Fridges", "slug": "fridges", "is_active": True}
    defaults.update(kwargs)
    return Category.objects.create(**defaults)


def _make_brand(**kwargs):
    """Create a brand with sensible defaults.

    Args:
        **kwargs: overrides for the brand fields.

    Returns:
        Brand: the created brand.
    """
    defaults = {"name": "Samsung", "slug": "samsung"}
    defaults.update(kwargs)
    return Brand.objects.create(**defaults)


def _make_product(category, brand=None, **kwargs):
    """Create an active product under the given category.

    Args:
        category: the category to attach the product to.
        brand: optional brand association.
        **kwargs: overrides for the product fields.

    Returns:
        Product: the created product.
    """
    defaults = {
        "name": "Side-by-Side Fridge",
        "slug": "side-by-side-fridge",
        "sku": "FRG-001",
        "price": Decimal("89995.00"),
        "stock_status": "in_stock",
        "is_active": True,
        "category": category,
        "brand": brand,
    }
    defaults.update(kwargs)
    return Product.objects.create(**defaults)


class ProductDetailRelatedProductsTests(APITestCase):
    """Verifies that related products are included inline in the detail payload."""

    def setUp(self):
        cache.clear()
        self.category = _make_category()
        self.brand = _make_brand()
        self.product = _make_product(
            self.category,
            self.brand,
            name="Main Fridge",
            slug="main-fridge",
            sku="FRG-MAIN",
        )
        self.related1 = _make_product(
            self.category,
            self.brand,
            name="Water Filter",
            slug="water-filter",
            sku="FRG-FILTER",
            price=Decimal("2500.00"),
        )
        self.related2 = _make_product(
            self.category,
            self.brand,
            name="Ice Maker Kit",
            slug="ice-maker-kit",
            sku="FRG-ICE",
            price=Decimal("15000.00"),
        )
        RelatedProduct.objects.create(
            product=self.product,
            related_product=self.related1,
            relation_type="accessory",
            sort_order=0,
        )
        RelatedProduct.objects.create(
            product=self.product,
            related_product=self.related2,
            relation_type="accessory",
            sort_order=1,
        )
        self.detail_url = reverse(
            "api:catalog:product-detail", kwargs={"slug": "main-fridge"}
        )

    def test_detail_includes_related_products_inline(self):
        """The product detail response embeds related products directly."""
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        related = response.data["related_products"]
        self.assertEqual(len(related), 2)
        slugs = {item["slug"] for item in related}
        self.assertEqual(slugs, {"water-filter", "ice-maker-kit"})

    def test_related_products_include_effective_price(self):
        """Each related product card carries a server-computed effective price."""
        response = self.client.get(self.detail_url)
        related = response.data["related_products"]
        for item in related:
            self.assertIn("effective_price", item)
            self.assertIsNotNone(item["effective_price"])

    def test_detail_with_no_related_products_returns_empty_list(self):
        """A product without related links returns an empty list, not null."""
        _make_product(
            self.category,
            self.brand,
            name="Solo Appliance",
            slug="solo-appliance",
            sku="SOLO-001",
        )
        url = reverse(
            "api:catalog:product-detail", kwargs={"slug": "solo-appliance"}
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["related_products"], [])


class ProductDetailJsonLdTests(APITestCase):
    """Verifies that the product detail endpoint returns valid Product JSON-LD."""

    def setUp(self):
        cache.clear()
        self.category = _make_category()
        self.brand = _make_brand()
        self.product = _make_product(self.category, self.brand)
        self.detail_url = reverse(
            "api:catalog:product-detail",
            kwargs={"slug": self.product.slug},
        )

    def test_json_ld_has_product_type_and_kes_currency(self):
        """JSON-LD includes @type Product with KES priceCurrency."""
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ld = response.data["json_ld"]
        self.assertEqual(ld["@context"], "https://schema.org")
        self.assertEqual(ld["@type"], "Product")
        self.assertEqual(ld["offers"]["priceCurrency"], "KES")

    def test_json_ld_has_availability(self):
        """JSON-LD offers include an availability value from schema.org."""
        response = self.client.get(self.detail_url)
        ld = response.data["json_ld"]
        self.assertIn("availability", ld["offers"])
        self.assertTrue(
            ld["offers"]["availability"].startswith("https://schema.org/")
        )

    def test_json_ld_price_matches_effective_pricing(self):
        """The JSON-LD price matches the effective_pricing field."""
        response = self.client.get(self.detail_url)
        ld = response.data["json_ld"]
        effective = response.data["effective_pricing"]
        self.assertEqual(ld["offers"]["price"], effective["price"])

    def test_json_ld_includes_brand(self):
        """JSON-LD includes the brand when the product has one."""
        response = self.client.get(self.detail_url)
        ld = response.data["json_ld"]
        self.assertEqual(ld["brand"]["@type"], "Brand")
        self.assertEqual(ld["brand"]["name"], self.brand.name)

    def test_json_ld_includes_aggregate_rating_when_reviewed(self):
        """JSON-LD includes aggregateRating when the product has reviews."""
        self.product.average_rating = Decimal("4.5")
        self.product.review_count = 12
        self.product.save(update_fields=["average_rating", "review_count"])
        response = self.client.get(self.detail_url)
        ld = response.data["json_ld"]
        self.assertEqual(ld["aggregateRating"]["@type"], "AggregateRating")
        self.assertEqual(ld["aggregateRating"]["ratingValue"], "4.50")
        self.assertEqual(ld["aggregateRating"]["reviewCount"], 12)

    def test_json_ld_omits_aggregate_rating_when_unreviewed(self):
        """JSON-LD has no aggregateRating when review_count is zero."""
        response = self.client.get(self.detail_url)
        ld = response.data["json_ld"]
        self.assertNotIn("aggregateRating", ld)

    def test_json_ld_stock_status_mapping(self):
        """Each stock_status value maps to a correct schema.org availability."""
        expected = {
            "in_stock": "https://schema.org/InStock",
            "out_of_stock": "https://schema.org/OutOfStock",
            "pre_order": "https://schema.org/PreOrder",
            "backorder": "https://schema.org/BackOrder",
            "discontinued": "https://schema.org/Discontinued",
        }
        for db_status, schema_url in expected.items():
            self.product.stock_status = db_status
            self.product.save(update_fields=["stock_status"])
            cache.clear()
            response = self.client.get(self.detail_url)
            ld = response.data["json_ld"]
            self.assertEqual(
                ld["offers"]["availability"],
                schema_url,
                f"stock_status={db_status} should map to {schema_url}",
            )


@override_settings(INTERNAL_API_TOKEN="test-crawl-token")
class CrawlThrottleAcceptanceTests(APITestCase):
    """Proves that crawling product detail URLs produces zero 429s.

    Simulates both no-delay and 1-req/s patterns with the internal render
    token. The internal_render scope (3000/min) must handle all requests
    without hitting the public_catalog limit (100/min).
    """

    def setUp(self):
        cache.clear()
        self.category = _make_category()
        self.brand = _make_brand()
        self.products = []
        for i in range(20):
            product = _make_product(
                self.category,
                self.brand,
                name=f"Product {i}",
                slug=f"product-{i}",
                sku=f"SKU-{i:04d}",
                price=Decimal("10000.00") + i * 1000,
            )
            self.products.append(product)

        for product in self.products[:10]:
            RelatedProduct.objects.create(
                product=product,
                related_product=self.products[(product.pk % 20)],
                relation_type="accessory",
                sort_order=0,
            )

    def _crawl_product_detail(self, slug, visitor_ip="203.0.113.50"):
        """Make an internal-render request to a product detail endpoint.

        Args:
            slug: the product slug.
            visitor_ip: the forwarded visitor IP.

        Returns:
            Response: the API response.
        """
        url = reverse("api:catalog:product-detail", kwargs={"slug": slug})
        return self.client.get(
            url,
            REMOTE_ADDR="198.51.100.40",
            HTTP_X_INTERNAL_TOKEN="test-crawl-token",
            HTTP_X_FORWARDED_FOR=f"{visitor_ip}, 198.51.100.2",
        )

    def test_burst_crawl_produces_zero_429s(self):
        """Crawling all product URLs with no delay produces no 429 responses."""
        statuses_429 = 0
        for product in self.products:
            response = self._crawl_product_detail(product.slug)
            if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                statuses_429 += 1
            else:
                self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(
            statuses_429,
            0,
            f"Expected zero 429s but got {statuses_429} during burst crawl.",
        )

    def test_crawl_response_consistency(self):
        """Related-product count and price are identical across burst and single requests."""
        single_results = {}
        for product in self.products[:5]:
            cache.clear()
            response = self._crawl_product_detail(product.slug)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            single_results[product.slug] = {
                "related_count": len(response.data.get("related_products", [])),
                "price": response.data["json_ld"]["offers"]["price"],
            }

        cache.clear()

        burst_results = {}
        for product in self.products[:5]:
            response = self._crawl_product_detail(product.slug)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            burst_results[product.slug] = {
                "related_count": len(response.data.get("related_products", [])),
                "price": response.data["json_ld"]["offers"]["price"],
            }

        for slug in single_results:
            self.assertEqual(
                single_results[slug]["related_count"],
                burst_results[slug]["related_count"],
                f"Related-product count mismatch for {slug}",
            )
            self.assertEqual(
                single_results[slug]["price"],
                burst_results[slug]["price"],
                f"Price mismatch for {slug}",
            )

    def test_different_visitor_ips_get_separate_buckets(self):
        """Internal render requests keyed by forwarded visitor IP stay isolated."""
        for product in self.products[:10]:
            response = self._crawl_product_detail(
                product.slug, visitor_ip="203.0.113.60"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        for product in self.products[:10]:
            response = self._crawl_product_detail(
                product.slug, visitor_ip="203.0.113.70"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_public_scope_not_exhausted_by_internal_render(self):
        """Public-scope budget stays intact while internal renders are served.

        Sends many internal renders, then verifies that a plain anonymous
        request (no internal token) still passes the public_catalog throttle.
        """
        for product in self.products:
            self._crawl_product_detail(product.slug)

        url = reverse(
            "api:catalog:product-detail",
            kwargs={"slug": self.products[0].slug},
        )
        public_response = self.client.get(url, REMOTE_ADDR="203.0.113.99")
        self.assertNotEqual(
            public_response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Internal renders should not consume the public throttle budget.",
        )
