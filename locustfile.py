"""Locust load-test scenario for the storefront API.

Run against a seeded environment::

    python manage.py seed_load_test_data --products 300 --orders 20000
    locust -f locustfile.py --host https://staging.example.com

The customer traffic profile is deliberately storefront-shaped: most hits are
list reads and browsing (categories, brands, products, featured collections),
a smaller share are detail pages, and search carries the "hot" text lookups.
Callers are anonymous — checkout and payment paths need authenticated,
idempotency-keyed traffic and are out of scope for this anonymous profile.

``on_start`` caches a handful of product and category slugs from the live
catalog so detail tasks hit real resources instead of 404s on fabricated
slugs, and falls back to a stable seed list if the catalog is empty.
"""

import random

from locust import HttpUser, between, task

PRODUCT_SLUG_FALLBACK = [
    "loadtest-product-0000",
    "loadtest-product-0001",
    "loadtest-product-0002",
]
CATEGORY_SLUG_FALLBACK = [
    "loadtest-fridges",
    "loadtest-washers",
    "loadtest-kitchen",
]
SEARCH_TERMS = ["fridge", "washer", "samsung", "energy"]
PAGE_SIZE = 24


class StorefrontUser(HttpUser):
    """Anonymous storefront browsing user."""

    wait_time = between(1.0, 3.0)

    def _get_json(self, path, params=None):
        """Fetch a page and raise so locust records a failure on non-200.

        Args:
            path (str): the API path.
            params (dict | None): query string parameters.

        Returns:
            dict | None: the parsed JSON body, or None on failure.
        """
        with self.client.get(path, params=params, catch_response=True) as resp:
            if resp.status_code == 200:
                return resp.json()
            resp.failure(f"HTTP {resp.status_code} for {path}")
            return None

    def on_start(self):
        """Warm the slug pools from the live catalog on first request."""
        data = self._get_json("/api/v1/products/", params={"page_size": PAGE_SIZE})
        self.product_slugs = []
        if data:
            self.product_slugs = [
                item["slug"] for item in data.get("results", []) if item.get("slug")
            ]
        if not self.product_slugs:
            self.product_slugs = list(PRODUCT_SLUG_FALLBACK)

        data = self._get_json("/api/v1/categories/")
        self.category_slugs = []
        if data:
            self.category_slugs = [item["slug"] for item in data if item.get("slug")]
        if not self.category_slugs:
            self.category_slugs = list(CATEGORY_SLUG_FALLBACK)

    @task(6)
    def list_categories(self, params=None):
        """Browse the category tree."""
        self.client.get("/api/v1/categories/", params=params)

    @task(4)
    def list_brands(self):
        """Browse the brand list."""
        self.client.get("/api/v1/brands/")

    @task(10)
    def list_products(self):
        """Browse products, paging through the first few pages."""
        page = random.randint(1, 4)
        self.client.get(
            "/api/v1/products/",
            params={"page": page, "page_size": PAGE_SIZE},
        )

    @task(8)
    def search_products(self):
        """Run a hot search phrase against the interim icontains search."""
        term = random.choice(SEARCH_TERMS)
        self.client.get(
            "/api/v1/products/",
            params={"search": term, "page_size": PAGE_SIZE},
        )

    @task(6)
    def product_detail(self):
        """Open a product detail page."""
        slug = random.choice(self.product_slugs)
        self.client.get(f"/api/v1/products/{slug}/")

    @task(3)
    def product_price(self):
        """Check a variant price (bundles/catalog pricing endpoint)."""
        slug = random.choice(self.product_slugs)
        self.client.get(f"/api/v1/products/{slug}/price/")

    @task(2)
    def category_detail(self):
        """Open a category page with its product counts."""
        slug = random.choice(self.category_slugs)
        self.client.get(f"/api/v1/categories/{slug}/")

    @task(3)
    def list_collections(self):
        """Browse the featured collections."""
        self.client.get("/api/v1/collections/")

    @task(2)
    def collection_products(self):
        """Open the product list of a collection."""
        data = self._get_json("/api/v1/collections/")
        if not data:
            return
        results = data.get("results") or data
        if not results:
            return
        collection = random.choice(results)
        self.client.get(f"/api/v1/collections/{collection['slug']}/products/")
