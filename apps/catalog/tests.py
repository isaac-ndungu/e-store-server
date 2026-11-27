"""Tests for the catalog app.

Covers public browse endpoints (categories, brands, products, search,
filtering, faceted aggregation), admin CRUD for every model, nested-resource
parent-ownership validation, inactive-product detail 404s, atomic multi-model
creation, facet definition validation and CRUD, image upload validation, and
the security / role-based separation between anonymous, customer, and admin
callers.
"""

import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog import cache as catalog_cache
from apps.catalog.models import (
    Brand,
    Category,
    FacetDefinition,
    PricingTier,
    Product,
    ProductFAQ,
    ProductImage,
    RelatedProduct,
)
from apps.social_proof.models import ProductViewEvent

URLS = {
    "categories": reverse("api:catalog:category-list"),
    "brands": reverse("api:catalog:brand-list"),
    "products": reverse("api:catalog:product-list"),
    "suggestions": reverse("api:catalog:product-suggestions"),
    "admin_categories": reverse("api:catalog:admin-category-list-create"),
    "admin_brands": reverse("api:catalog:admin-brand-list-create"),
    "admin_products": reverse("api:catalog:admin-product-list-create"),
    "admin_facets": reverse("api:catalog:admin-facet-list-create"),
}

# The catalog public scope is configured as 100/min in settings.py; keep in sync.
PUBLIC_CATALOG_RATE_LIMIT = 100


def _make_user(password="StrongPass123!", **kwargs):
    """Create a user with defaults appropriate for catalog tests."""
    return User.objects.create_user(
        email=kwargs.pop("email", "buyer@example.com"),
        username=kwargs.pop("username", "buyer"),
        password=password,
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


def _unique_slug(model, value):
    """Return a URL-safe slug for ``value`` that is unique in the test DB.

    Args:
        model (type): the Django model to check slug uniqueness against.
        value (str): the source text.

    Returns:
        str: a unique slug.
    """
    base = slugify(value)
    slug = base
    counter = 1
    while model.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _unique_sku(value):
    """Return a ``Product`` SKU that is unique in the test DB.

    Args:
        value (str): the base SKU.

    Returns:
        str: a unique SKU.
    """
    sku = value
    counter = 1
    while Product.objects.filter(sku=sku).exists():
        sku = f"{value}-{counter}"
        counter += 1
    return sku


def _make_category(**kwargs):
    """Create a test category and return it."""
    name = kwargs.get("name", "Refrigerators")
    slug = kwargs.get("slug") or _unique_slug(Category, name)
    return Category.objects.create(
        name=name,
        slug=slug,
        is_active=kwargs.get("is_active", True),
        parent=kwargs.get("parent"),
        sort_order=kwargs.get("sort_order", 0),
    )


def _make_brand(**kwargs):
    """Create a test brand and return it."""
    name = kwargs.get("name", "Samsung")
    slug = kwargs.get("slug") or _unique_slug(Brand, name)
    return Brand.objects.create(
        name=name,
        slug=slug,
    )


def _make_product(**kwargs):
    """Create a test product and return it."""
    category = kwargs.get("category") or _make_category()
    brand = kwargs.get("brand") or _make_brand()
    name = kwargs.get("name", "Fridge 200L")
    slug = kwargs.get("slug") or _unique_slug(Product, name)
    sku = kwargs.get("sku") or _unique_sku("FRG-200")
    return Product.objects.create(
        name=name,
        slug=slug,
        sku=sku,
        description=kwargs.get("description", "A 200L refrigerator."),
        category=category,
        brand=brand,
        is_active=kwargs.get("is_active", True),
        is_discontinued=kwargs.get("is_discontinued", False),
        specs=kwargs.get("specs", {"capacity": "200L", "energy_rating": "A"}),
        price=kwargs.get("price", "45000.00"),
        stock_status=kwargs.get("stock_status", "in_stock"),
    )


def _png_upload(width=1200, height=900, name="photo.png", color=(255, 0, 0)):
    """Return an in-memory PNG of the given dimensions as an upload."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/png")


def _pdf_upload(name="manual.pdf"):
    """Return a minimal in-memory PDF as an upload."""
    content = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF"
    return SimpleUploadedFile(name, content, content_type="application/pdf")


class CategoryBrowseTests(APITestCase):
    """Exercises the public category list/detail endpoints."""

    def setUp(self):
        cache.clear()

    def test_categories_are_public(self):
        """An anonymous caller can list active categories."""
        _make_category()
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_inactive_categories_excluded_from_list(self):
        """Inactive categories do not appear in the public list."""
        _make_category(name="Active", slug="active-cat")
        _make_category(name="Hidden", slug="hidden-cat", is_active=False)
        response = self.client.get(URLS["categories"])
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["slug"], "active-cat")

    def test_category_detail_returns_children(self):
        """A category detail includes nested sub-categories and product count."""
        parent = _make_category()
        Category.objects.create(
            name="Two Door",
            slug="two-door",
            parent=parent,
            is_active=True,
        )
        url = reverse("api:catalog:category-detail", args=[parent.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["children"]), 1)
        self.assertEqual(response.data["children"][0]["slug"], "two-door")

    def test_inactive_category_detail_returns_404(self):
        """An inactive category is not retrievable by slug (public)."""
        _make_category(name="Hidden", slug="hidden-cat", is_active=False)
        url = reverse("api:catalog:category-detail", args=["hidden-cat"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_missing_category_returns_404(self):
        """A nonexistent category slug returns 404."""
        url = reverse("api:catalog:category-detail", args=["no-such-cat"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_categories_throttled_at_public_catalog_scope(self):
        """Bursting past the public_catalog rate limit yields HTTP 429."""
        _make_category()
        for _ in range(PUBLIC_CATALOG_RATE_LIMIT):
            self.client.get(URLS["categories"])
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_non_numeric_parent_param_rejected(self):
        """A non-numeric ``parent`` param returns 400, not a 500."""
        response = self.client.get(URLS["categories"], {"parent": "abc"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_numeric_parent_param_accepted(self):
        """A numeric ``parent`` param filters to that category's children."""
        parent = _make_category()
        child = _make_category(name="Two Door", slug="two-door-child")
        child.parent = parent
        child.save()
        response = self.client.get(URLS["categories"], {"parent": parent.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["slug"], "two-door-child")

    def test_null_parent_param_lists_top_level(self):
        """The ``null`` parent marker lists only root categories."""
        _make_category(name="Root One", slug="root-one")
        root_two = _make_category(name="Root Two", slug="root-two")
        Category.objects.create(
            name="Child",
            slug="nested-child",
            parent=root_two,
            is_active=True,
        )
        response = self.client.get(URLS["categories"], {"parent": "null"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slugs = {c["slug"] for c in response.data}
        self.assertEqual(slugs, {"root-one", "root-two"})

    def test_categories_follow_manual_sort_order(self):
        """Categories are ordered by ``sort_order`` then name."""
        _make_category(name="Zeta", slug="zeta", sort_order=2)
        _make_category(name="Alpha", slug="alpha", sort_order=2)
        _make_category(name="First", slug="first", sort_order=0)
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [category["slug"] for category in response.data],
            ["first", "alpha", "zeta"],
        )
        self.assertEqual(response.data[0]["sort_order"], 0)

    def test_category_detail_children_follow_manual_sort_order(self):
        """A category's children are ordered by ``sort_order`` then name."""
        parent = _make_category(name="Audio", slug="audio")
        Category.objects.create(
            name="Speakers",
            slug="speakers",
            parent=parent,
            sort_order=2,
            is_active=True,
        )
        Category.objects.create(
            name="Soundbars",
            slug="soundbars",
            parent=parent,
            sort_order=1,
            is_active=True,
        )
        url = reverse("api:catalog:category-detail", args=[parent.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [child["slug"] for child in response.data["children"]],
            ["soundbars", "speakers"],
        )


class CategoryCacheTests(APITestCase):
    """Exercises the category cache and its invalidation on data changes."""

    def setUp(self):
        cache.clear()
        self.category = _make_category(name="Refrigerators", slug="refrigerators")

    def test_list_rows_cached_after_first_read(self):
        """A list read populates the cache under the current generation."""
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        generation = catalog_cache.get_category_generation()
        self.assertEqual(
            catalog_cache.get_cached_category_rows(generation), response.data
        )

    def test_category_rename_invalidates_list_cache(self):
        """Editing a category is reflected on the storefront immediately."""
        self.client.get(URLS["categories"])
        self.category.name = "Fridges"
        self.category.save(update_fields=["name"])
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.data[0]["name"], "Fridges")

    def test_category_delete_invalidates_list_cache(self):
        """Deleting a category removes it from the storefront immediately."""
        self.client.get(URLS["categories"])
        self.category.delete()
        response = self.client.get(URLS["categories"])
        self.assertEqual(len(response.data), 0)

    def test_category_inactivation_invalidates_list_cache(self):
        """Deactivating a category hides it without a cache expiry wait."""
        self.client.get(URLS["categories"])
        self.category.is_active = False
        self.category.save(update_fields=["is_active"])
        response = self.client.get(URLS["categories"])
        self.assertEqual(len(response.data), 0)

    def test_product_count_invalidation_on_create(self):
        """A new product is counted in the next list read."""
        self.client.get(URLS["categories"])
        _make_product(category=self.category, slug="cache-prod", sku="CACHE-1")
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.data[0]["product_count"], 1)

    def test_product_count_invalidation_on_delete(self):
        """Removing a product restores the previous count."""
        _make_product(category=self.category, slug="cache-prod", sku="CACHE-1")
        self.client.get(URLS["categories"])
        self.assertEqual(
            self.client.get(URLS["categories"]).data[0]["product_count"], 1
        )
        Product.objects.get(slug="cache-prod").delete()
        response = self.client.get(URLS["categories"])
        self.assertEqual(response.data[0]["product_count"], 0)

    def test_detail_cached_then_invalidated_on_edit(self):
        """A category detail is cached and refreshed when the category changes."""
        url = reverse("api:catalog:category-detail", args=[self.category.slug])
        first = self.client.get(url)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        generation = catalog_cache.get_category_generation()
        self.assertEqual(
            catalog_cache.get_cached_category_detail(self.category.slug, generation),
            first.data,
        )
        self.category.name = "Fridges"
        self.category.save(update_fields=["name"])
        second = self.client.get(url)
        self.assertEqual(second.data["name"], "Fridges")


class BrandBrowseTests(APITestCase):
    """Exercises the public brand list/detail endpoints."""

    def setUp(self):
        cache.clear()

    def test_brands_are_public(self):
        """An anonymous caller can list brands."""
        _make_brand()
        response = self.client.get(URLS["brands"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_brand_detail(self):
        """A brand detail is retrievable by slug."""
        _make_brand()
        url = reverse("api:catalog:brand-detail", args=["samsung"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Samsung")

    def test_missing_brand_returns_404(self):
        """A nonexistent brand slug returns 404."""
        url = reverse("api:catalog:brand-detail", args=["no-such-brand"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class QueryCountTests(APITestCase):
    """Guards against N+1 regressions in public list endpoints."""

    def setUp(self):
        cache.clear()
        self.category = _make_category()
        self.brand = _make_brand()
        for i in range(6):
            Product.objects.create(
                name=f"Product {i}",
                slug=f"qcount-{i}",
                sku=f"QCOUNT-{i}",
                category=self.category,
                brand=self.brand,
                price="1000.00",
            )

    def test_category_list_uses_single_query(self):
        """The category list renders without a count query per row."""
        _make_category(name="Second", slug="qcount-second")
        with self.assertNumQueries(1):
            response = self.client.get(URLS["categories"])
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]["product_count"], 6)

    def test_brand_list_uses_single_query(self):
        """The brand list renders without a count query per row."""
        with self.assertNumQueries(1):
            response = self.client.get(URLS["brands"])
        self.assertEqual(response.data[0]["product_count"], 6)


class ProductListBrowseTests(APITestCase):
    """Exercises the public product search/list/filter/order endpoints."""

    def setUp(self):
        cache.clear()
        self.category = _make_category()  # slug "refrigerators"
        self.washer_category = _make_category(name="Laundry", slug="laundry")
        self.brand = _make_brand()  # slug "samsung"
        _make_product(
            name="Fridge 200L",
            slug="fridge-200l",
            sku="FRG-200",
            category=self.category,
            brand=self.brand,
        )
        _make_product(
            name="Fridge 300L",
            slug="fridge-300l",
            sku="FRG-300",
            category=self.category,
            brand=self.brand,
            specs={"capacity": "300L", "energy_rating": "B"},
        )
        _make_product(
            name="Washer",
            slug="washer",
            sku="WSH-100",
            category=self.washer_category,
            brand=self.brand,
            description="A washing machine.",
            specs={"capacity": "8kg"},
        )

    def test_products_list_is_public_and_paginated(self):
        """An anonymous caller can list products with paginated results."""
        response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("count", response.data)
        self.assertIn("results", response.data)
        self.assertEqual(response.data["count"], 3)

    def test_search_by_name(self):
        """Free-text search matches the product name."""
        response = self.client.get(URLS["products"], {"search": "Fridge"})
        self.assertEqual(response.data["count"], 2)
        slugs = {p["slug"] for p in response.data["results"]}
        self.assertEqual(slugs, {"fridge-200l", "fridge-300l"})

    def test_search_by_sku(self):
        """Free-text search matches the product SKU."""
        response = self.client.get(URLS["products"], {"search": "FRG-300"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "fridge-300l")

    def test_blank_search_returns_everything(self):
        """A whitespace-only search is treated as no search at all."""
        response = self.client.get(URLS["products"], {"search": "   "})
        self.assertEqual(response.data["count"], 3)

    def test_search_matches_brand_name(self):
        """A brand name search returns that brand's products."""
        response = self.client.get(URLS["products"], {"search": "samsung"})
        self.assertEqual(response.data["count"], 3)

    def test_search_synonyms(self):
        """Common synonyms match the catalog's preferred wording."""
        _make_product(
            name="Refrigerator 500L",
            slug="refrigerator-500l",
            sku="REF-500",
            description="Cold storage for a large household.",
        )
        response = self.client.get(URLS["products"], {"search": "fridge"})
        slugs = {p["slug"] for p in response.data["results"]}
        self.assertIn("refrigerator-500l", slugs)

    def test_search_tolerates_typos_on_postgres(self):
        """A one-character typo still finds the product on PostgreSQL."""
        if connection.vendor != "postgresql":
            self.skipTest("Trigram matching applies only on PostgreSQL.")
        response = self.client.get(URLS["products"], {"search": "frige"})
        slugs = {p["slug"] for p in response.data["results"]}
        self.assertIn("fridge-200l", slugs)

    def test_search_filter_is_backed_by_trigram_indexes_on_postgres(self):
        """Fuzzy search uses the ``%`` operator and has GIN indexes to serve it."""
        if connection.vendor != "postgresql":
            self.skipTest("Trigram indexes exist only on PostgreSQL.")
        from apps.catalog.search import filter_products_by_search

        sql = str(filter_products_by_search(Product.objects.all(), "frige").query)
        self.assertIn("%", sql)

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, Product._meta.db_table
            )
        for index_name in (
            "prod_name_trgm_idx",
            "prod_sku_trgm_idx",
            "prod_short_desc_trgm_idx",
            "prod_desc_trgm_idx",
        ):
            self.assertIn(index_name, constraints)

    def test_filter_in_stock(self):
        """``in_stock=true`` hides out-of-stock products; false does not."""
        Product.objects.filter(slug="washer").update(stock_status="out_of_stock")
        in_stock = self.client.get(URLS["products"], {"in_stock": "true"})
        self.assertEqual(in_stock.data["count"], 2)
        all_products = self.client.get(URLS["products"], {"in_stock": "false"})
        self.assertEqual(all_products.data["count"], 3)

    def test_filter_by_price_range(self):
        """``price_min`` and ``price_max`` bound the base price."""
        Product.objects.filter(slug="washer").update(price=Decimal("30000.00"))
        Product.objects.filter(slug="fridge-300l").update(price=Decimal("60000.00"))
        response = self.client.get(
            URLS["products"], {"price_min": "40000", "price_max": "50000"}
        )
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "fridge-200l")

    def test_order_by_popularity_uses_recent_views(self):
        """Popular ordering counts recent durable view events only."""
        from django.utils import timezone

        washer = Product.objects.get(slug="washer")
        fridge = Product.objects.get(slug="fridge-200l")
        stale = Product.objects.get(slug="fridge-300l")
        for index in range(3):
            ProductViewEvent.objects.create(
                product=washer, session_key=f"recent-{index}"
            )
        ProductViewEvent.objects.create(product=fridge, session_key="recent-fridge")
        old_date = timezone.now() - timedelta(days=60)
        for index in range(2):
            event = ProductViewEvent.objects.create(
                product=stale, session_key=f"stale-{index}"
            )
            ProductViewEvent.objects.filter(pk=event.pk).update(created_at=old_date)

        response = self.client.get(URLS["products"], {"ordering": "popular"})
        self.assertEqual(
            [p["slug"] for p in response.data["results"]],
            ["washer", "fridge-200l", "fridge-300l"],
        )

    def test_page_size_is_bounded(self):
        """Callers can request small pages but not unbounded ones."""
        small = self.client.get(URLS["products"], {"page_size": 2})
        self.assertEqual(len(small.data["results"]), 2)
        huge = self.client.get(URLS["products"], {"page_size": 500})
        self.assertEqual(len(huge.data["results"]), 3)

    def test_inactive_products_excluded_from_list(self):
        """Inactive and discontinued products do not appear in the list."""
        _make_product(name="Hidden", slug="hidden", sku="HID-1", is_active=False)
        _make_product(
            name="Old",
            slug="old-model",
            sku="OLD-1",
            is_discontinued=True,
        )
        response = self.client.get(URLS["products"])
        self.assertEqual(response.data["count"], 3)

    def test_filter_by_category(self):
        """Products can be filtered by category id."""
        cat = Category.objects.get(slug="refrigerators")
        response = self.client.get(URLS["products"], {"category": cat.id})
        self.assertEqual(response.data["count"], 2)

    def test_filter_by_brand(self):
        """Products can be filtered by brand id."""
        brand = Brand.objects.get(slug="samsung")
        response = self.client.get(URLS["products"], {"brand": brand.id})
        self.assertEqual(response.data["count"], 3)

    def test_filter_by_parent_category_includes_descendants(self):
        """A parent category lists products assigned to its subcategories."""
        parent = _make_category(name="Kitchen", slug="kitchen")
        child = _make_category(name="Blenders", slug="blenders", parent=parent)
        _make_product(name="Blender", slug="blender", sku="BLN-1", category=child)
        response = self.client.get(URLS["products"], {"category": parent.id})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "blender")

    def test_ordering_by_price(self):
        """Products can be ordered by base price in either direction."""
        Product.objects.filter(slug="washer").update(price=Decimal("30000.00"))
        Product.objects.filter(slug="fridge-300l").update(price=Decimal("60000.00"))

        ascending = self.client.get(URLS["products"], {"ordering": "price"})
        self.assertEqual(
            [p["slug"] for p in ascending.data["results"]],
            ["washer", "fridge-200l", "fridge-300l"],
        )

        descending = self.client.get(URLS["products"], {"ordering": "-price"})
        self.assertEqual(
            [p["slug"] for p in descending.data["results"]],
            ["fridge-300l", "fridge-200l", "washer"],
        )

    def test_ordering_by_name(self):
        """Products can be ordered by name."""
        response = self.client.get(URLS["products"], {"ordering": "name"})
        names = [p["name"] for p in response.data["results"]]
        self.assertEqual(names, sorted(names))

    def test_explicit_ordering_wins_over_search_relevance(self):
        """An explicit ordering param is honoured on a text search."""
        response = self.client.get(
            URLS["products"], {"search": "Fridge", "ordering": "name"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        names = [p["name"] for p in response.data["results"]]
        self.assertEqual(names, sorted(names))

    def test_search_rank_puts_exact_name_first_on_postgres(self):
        """The exact name match ranks above a similar sibling product."""
        if connection.vendor != "postgresql":
            self.skipTest("Relevance ranking applies only on PostgreSQL.")
        response = self.client.get(URLS["products"], {"search": "Fridge 300L"})
        self.assertEqual(response.data["results"][0]["slug"], "fridge-300l")

    def test_search_rank_is_noop_off_postgres(self):
        """Off PostgreSQL the relevance helper leaves the order unchanged."""
        from apps.catalog.search import order_products_by_search

        if connection.vendor == "postgresql":
            self.skipTest("Pass-through applies only off PostgreSQL.")
        queryset = Product.objects.all()
        self.assertEqual(
            list(
                order_products_by_search(queryset, "Fridge").values_list(
                    "pk", flat=True
                )
            ),
            list(queryset.values_list("pk", flat=True)),
        )

    def test_primary_image_included_in_list(self):
        """The product list exposes the primary image for each product."""
        product = Product.objects.get(slug="fridge-200l")
        ProductImage.objects.create(
            product=product,
            image="products/images/hero.png",
            is_primary=True,
            sort_order=0,
        )
        response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = next(r for r in response.data["results"] if r["slug"] == "fridge-200l")
        self.assertTrue(result["primary_image"].endswith("hero.png"))
        other = next(r for r in response.data["results"] if r["slug"] == "washer")
        self.assertIsNone(other["primary_image"])


class ProductListCacheTests(APITestCase):
    """Exercises the product-list page cache and its invalidation."""

    def setUp(self):
        cache.clear()
        self.category = _make_category()
        self.brand = _make_brand()
        _make_product(
            name="Fridge 200L",
            slug="fridge-200l",
            sku="FRG-200",
            category=self.category,
            brand=self.brand,
        )
        laundry = _make_category(name="Laundry", slug="laundry")
        _make_product(
            name="Washer",
            slug="washer",
            sku="WSH-100",
            description="A washing machine.",
            category=laundry,
            brand=self.brand,
        )

    def test_second_identical_request_serves_no_db_queries(self):
        """A warm page costs cache reads, not the count/select/aggregates."""
        first = self.client.get(URLS["products"])
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        with self.assertNumQueries(0):
            second = self.client.get(URLS["products"])
        self.assertEqual(second.data, first.data)

    def test_product_edit_invalidates_page_cache(self):
        """Renaming a product is reflected on the storefront immediately."""
        self.client.get(URLS["products"])
        product = Product.objects.get(slug="fridge-200l")
        product.name = "Fridge 200L Updated"
        product.save(update_fields=["name"])
        response = self.client.get(URLS["products"])
        names = {p["name"] for p in response.data["results"]}
        self.assertIn("Fridge 200L Updated", names)

    def test_new_product_appears_without_wait(self):
        """A created product is listed without a cache expiry wait."""
        self.assertEqual(self.client.get(URLS["products"]).data["count"], 2)
        _make_product(name="Dryer", slug="dryer", sku="DRY-100")
        self.assertEqual(self.client.get(URLS["products"]).data["count"], 3)

    def test_distinct_filters_cached_separately(self):
        """Different filter combinations do not share a cached page."""
        other_category = _make_category(name="Cooking", slug="cooking")
        plain = self.client.get(URLS["products"])
        filtered = self.client.get(URLS["products"], {"category": other_category.id})
        self.assertEqual(plain.data["count"], 2)
        self.assertEqual(filtered.data["count"], 0)
        self.assertEqual(self.client.get(URLS["products"]).data["count"], 2)

    def test_search_results_cached_per_term(self):
        """Each search term caches its own result set."""
        fridge = self.client.get(URLS["products"], {"search": "Fridge"})
        washer = self.client.get(URLS["products"], {"search": "Washer"})
        self.assertEqual(fridge.data["count"], 1)
        self.assertEqual(washer.data["count"], 1)
        repeat = self.client.get(URLS["products"], {"search": "Fridge"})
        self.assertEqual(repeat.data, fridge.data)

    def test_product_image_change_invalidates_page_cache(self):
        """A new primary image appears on the list card immediately."""
        self.client.get(URLS["products"])
        product = Product.objects.get(slug="fridge-200l")
        ProductImage.objects.create(
            product=product,
            image="products/images/hero.png",
            is_primary=True,
            sort_order=0,
        )
        response = self.client.get(URLS["products"])
        result = next(r for r in response.data["results"] if r["slug"] == "fridge-200l")
        self.assertTrue(result["primary_image"].endswith("hero.png"))

    def test_facet_definition_change_invalidates_page_cache(self):
        """A new facet definition refreshes the cached facets payload."""
        first = self.client.get(URLS["products"])
        self.assertEqual(first.data["facets"], {})
        FacetDefinition.objects.create(
            name="Capacity",
            key="capacity",
            source_field="product_specs",
            facet_type="choice",
            is_active=True,
        )
        second = self.client.get(URLS["products"])
        self.assertIn("Capacity", second.data["facets"])


class ProductSuggestionTests(APITestCase):
    """Exercises the storefront search autocomplete endpoint."""

    def setUp(self):
        cache.clear()
        self.category = _make_category(name="Refrigerators", slug="refrigerators")
        self.brand = _make_brand()
        _make_product(
            name="Fridge 200L",
            slug="fridge-200l",
            sku="FRG-200",
            category=self.category,
            brand=self.brand,
        )
        laundry = _make_category(name="Laundry", slug="laundry")
        _make_product(
            name="Washer",
            slug="washer",
            sku="WSH-100",
            description="A washing machine.",
            category=laundry,
        )

    def test_short_query_returns_empty_payload(self):
        """Queries under two normalized characters do not hit the catalog."""
        response = self.client.get(URLS["suggestions"], {"q": "f"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["products"], [])
        self.assertEqual(response.data["categories"], [])

    def test_matching_products_and_categories(self):
        """Products and categories matching the query are both returned."""
        response = self.client.get(URLS["suggestions"], {"q": "fridge"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["query"], "fridge")
        self.assertEqual(
            {p["slug"] for p in response.data["products"]}, {"fridge-200l"}
        )
        self.assertEqual(
            {c["slug"] for c in response.data["categories"]}, {"refrigerators"}
        )
        product = response.data["products"][0]
        self.assertEqual(
            set(product),
            {
                "id",
                "name",
                "slug",
                "price",
                "compare_at_price",
                "primary_image",
                "brand_name",
            },
        )

    def test_suggestion_payload_is_cached(self):
        """A repeated query is served from the short-lived suggestion cache."""
        first = self.client.get(URLS["suggestions"], {"q": "fridge"})
        with self.assertNumQueries(0):
            second = self.client.get(URLS["suggestions"], {"q": "fridge"})
        self.assertEqual(second.data, first.data)

    def test_suggestion_uses_own_throttle_scope(self):
        """The autocomplete endpoint stays off the stricter catalog budget."""
        from apps.catalog.views import ProductSuggestionView

        self.assertEqual(ProductSuggestionView.throttle_scope, "catalog_suggest")


class ProductDetailBrowseTests(APITestCase):
    """Exercises the public product detail and pricing endpoints."""

    def setUp(self):
        cache.clear()
        self.product = _make_product()

    def test_product_detail_is_public_and_includes_pricing(self):
        """A product detail returns nested pricing tiers and metadata."""
        PricingTier.objects.create(
            product=self.product, min_quantity=5, unit_price="41000.00"
        )
        url = reverse("api:catalog:product-detail", args=[self.product.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Fridge 200L")
        self.assertEqual(response.data["price"], "45000.00")
        self.assertEqual(len(response.data["pricing_tiers"]), 1)
        self.assertEqual(response.data["pricing_tiers"][0]["min_quantity"], 5)
        self.assertEqual(response.data["pricing_tiers"][0]["unit_price"], "41000.00")

    def test_inactive_product_detail_returns_404(self):
        """An inactive product is not retrievable by slug publicly."""
        inactive = _make_product(
            name="Hidden Fridge",
            slug="hidden-fridge",
            sku="HID-FRG",
            is_active=False,
        )
        url = reverse("api:catalog:product-detail", args=[inactive.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_discontinued_product_detail_returns_404(self):
        """A discontinued product is not retrievable by slug publicly."""
        old = _make_product(
            name="Old Fridge",
            slug="old-fridge",
            sku="OLD-FRG",
            is_discontinued=True,
        )
        url = reverse("api:catalog:product-detail", args=[old.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_price_endpoint_returns_product_pricing(self):
        """The price endpoint returns base and tier pricing for the product."""
        PricingTier.objects.create(
            product=self.product, min_quantity=5, unit_price="41000.00"
        )
        url = reverse("api:catalog:product-price", args=[self.product.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["product_slug"], self.product.slug)
        entry = response.data["product"]
        self.assertEqual(entry["price"], "45000.00")
        self.assertEqual(entry["base_price"], "45000.00")
        self.assertEqual(
            entry["pricing_tiers"],
            [{"min_quantity": 5, "unit_price": "41000.00"}],
        )

    def test_price_endpoint_404_for_inactive_product(self):
        """The price endpoint hides inactive products."""
        inactive = _make_product(
            name="Hidden", slug="hidden-price", sku="HID-PR", is_active=False
        )
        url = reverse("api:catalog:product-price", args=[inactive.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_price_endpoint_reflects_active_discount(self):
        """The advertised price is the discount-reduced effective price.

        A client must never be shown the raw stored price when a sitewide
        discount applies  -  the endpoint advertises the discounted amount and
        reports the undiscounted base and saving for comparison.
        """
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from apps.promotions.models import Discount

        Discount.objects.create(
            name="Launch",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=timezone.now() - timedelta(days=1),
        )
        cache.clear()
        url = reverse("api:catalog:product-price", args=[self.product.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry = response.data["product"]
        self.assertEqual(entry["base_price"], str(self.product.price))
        self.assertEqual(entry["price"], "40500.00")
        self.assertEqual(entry["discount"], "4500.00")

    def test_price_endpoint_query_bound(self):
        """Tier and discount data are prefetched, not queried per row.

        One product query, one tier query, and discount queries  -  the
        discount query resolves the effective (discounted) price server-side.
        """
        product = _make_product(name="Price Bound", slug="price-bound", sku="PR-BND")
        PricingTier.objects.create(
            product=product, min_quantity=5, unit_price="41000.00"
        )
        url = reverse("api:catalog:product-price", args=[product.slug])
        with self.assertNumQueries(4):
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class FacetSearchTests(APITestCase):
    """Exercises faceted aggregation on the product list."""

    def setUp(self):
        cache.clear()
        FacetDefinition.objects.create(
            name="Capacity",
            key="capacity",
            source_field="product_specs",
            facet_type="choice",
            is_active=True,
        )
        FacetDefinition.objects.create(
            name="Color",
            key="color",
            source_field="product_specs",
            facet_type="choice",
            is_active=True,
        )
        FacetDefinition.objects.create(
            name="Condition",
            field_name="condition",
            source_field="product_field",
            facet_type="choice",
            is_active=True,
        )
        _make_product(
            name="Fridge 200L",
            slug="fac-fridge-200",
            sku="FAC-200",
            specs={"capacity": "200L", "energy_rating": "A", "color": "Silver"},
        )
        _make_product(
            name="Fridge 300L",
            slug="fac-fridge-300",
            sku="FAC-300",
            specs={"capacity": "300L", "energy_rating": "B", "color": "Black"},
        )

    def test_response_includes_facet_counts(self):
        """The product list response carries a facets dict."""
        response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("facets", response.data)
        self.assertIn("Capacity", response.data["facets"])
        self.assertEqual(response.data["facets"]["Capacity"]["200L"], 1)
        self.assertEqual(response.data["facets"]["Capacity"]["300L"], 1)
        self.assertEqual(response.data["facets"]["Color"]["Silver"], 1)
        self.assertEqual(response.data["facets"]["Color"]["Black"], 1)

    def test_inactive_facets_excluded(self):
        """Inactive facet definitions do not appear in the response."""
        FacetDefinition.objects.filter(name="Color").update(is_active=False)
        response = self.client.get(URLS["products"])
        self.assertNotIn("Color", response.data["facets"])

    def test_relational_field_facet_counts(self):
        """Relational-field facets produce counts too."""
        response = self.client.get(URLS["products"])
        self.assertIn("Condition", response.data["facets"])
        self.assertEqual(response.data["facets"]["Condition"]["new"], 2)

    def test_facet_query_param_filters_results(self):
        """A known facet query param narrows results and counts."""
        response = self.client.get(URLS["products"], {"capacity": "200L"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "fac-fridge-200")

    def test_product_list_includes_effective_server_price(self):
        """List rows include the effective price after active discounts."""
        from apps.promotions.models import Discount

        Discount.objects.create(
            name="Catalog sale",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=timezone.now() - timedelta(days=1),
        )
        cache.clear()
        response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prices = {
            item["slug"]: item["effective_price"] for item in response.data["results"]
        }
        self.assertEqual(prices["fridge-200l"], "45000.00")

    def test_discount_change_invalidates_cached_product_list_prices(self):
        """A new discount retires product-list pages with stale effective prices."""
        from apps.promotions.models import Discount

        first = self.client.get(URLS["products"])
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        first_item = first.data["results"][0]
        base_price = Product.objects.get(slug=first_item["slug"]).price
        self.assertEqual(first_item["effective_price"], str(base_price))
        Discount.objects.create(
            name="Catalog sale",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=timezone.now() - timedelta(days=1),
        )
        second = self.client.get(URLS["products"])
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        second_prices = {
            item["slug"]: item["effective_price"] for item in second.data["results"]
        }
        self.assertEqual(
            second_prices[first_item["slug"]],
            str(base_price * Decimal("0.90")),
        )

    def test_unknown_facet_param_is_ignored(self):
        """An unrecognised facet param silently does not affect results."""
        response = self.client.get(URLS["products"], {"not_a_facet": "x"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_facet_counts_query_bound(self):
        """Facet counts cost one aggregate query per facet, not one per value."""
        for i in range(12):
            Product.objects.create(
                name=f"Bulk Fridge {i}",
                slug=f"fac-bulk-{i}",
                sku=f"FAC-BULK-{i}",
                specs={"capacity": f"{600 + i}L"},
                price="1000.00",
            )
        from apps.promotions.models import Discount

        Discount.objects.create(
            name="Catalog sale",
            scope="sitewide",
            discount_type="percent",
            value=Decimal("10.00"),
            starts_at=timezone.now() - timedelta(days=1),
        )
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Main query, image prefetch, pagination count, facet-definition
        # lookup, and one grouped aggregate per active facet (3 here).
        self.assertLessEqual(len(ctx.captured_queries), 14)
        self.assertEqual(response.data["facets"]["Capacity"]["611L"], 1)


class CategoryAdminTests(APITestCase):
    """Exercises admin category CRUD and permissions."""

    def setUp(self):
        cache.clear()
        _make_admin()

    def test_anonymous_cannot_list_create_categories(self):
        """An unauthenticated caller cannot manage categories."""
        response = self.client.get(URLS["admin_categories"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(
            URLS["admin_categories"], {"name": "Cat"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_create_category(self):
        """A plain non-staff token is rejected from category creation."""
        _make_user()
        self.client.force_authenticate(user=User.objects.get(email="buyer@example.com"))
        response = self.client.post(
            URLS["admin_categories"], {"name": "Cat"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Category.objects.count(), 0)

    def test_admin_can_create_category_with_autoslug(self):
        """An admin can create a category and its slug is auto-generated."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_categories"], {"name": "Microwaves"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], "microwaves")

    def test_admin_can_create_category_with_sort_order(self):
        """An admin can set the manual display order when creating a category."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_categories"],
            {"name": "Soundbars", "sort_order": 4},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["sort_order"], 4)
        self.assertEqual(Category.objects.get(slug="soundbars").sort_order, 4)

    def test_admin_can_update_category(self):
        """An admin can update a category."""
        _login(self.client)
        category = _make_category(name="Old Name", slug="old-name")
        url = reverse("api:catalog:admin-category-detail", args=[category.pk])
        response = self.client.patch(
            url, {"name": "New Name", "sort_order": 7}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        category.refresh_from_db()
        self.assertEqual(category.name, "New Name")
        self.assertEqual(category.sort_order, 7)

    def test_admin_can_delete_category(self):
        """An admin can delete a category."""
        _login(self.client)
        category = _make_category()
        url = reverse("api:catalog:admin-category-detail", args=[category.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Category.objects.count(), 0)


class BrandAdminTests(APITestCase):
    """Exercises admin brand CRUD and permissions."""

    def setUp(self):
        cache.clear()
        _make_admin()

    def test_anonymous_cannot_create_brand(self):
        """An unauthenticated caller cannot create a brand."""
        response = self.client.post(
            URLS["admin_brands"], {"name": "Brand"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_admin_can_create_brand_with_autoslug(self):
        """An admin can create a brand and its slug is auto-generated."""
        _login(self.client)
        response = self.client.post(URLS["admin_brands"], {"name": "LG"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], "lg")

    def test_admin_can_update_brand(self):
        """An admin can update a brand."""
        _login(self.client)
        brand = _make_brand(name="Old Brand", slug="old-brand")
        url = reverse("api:catalog:admin-brand-detail", args=[brand.pk])
        response = self.client.patch(url, {"name": "New Brand"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        brand.refresh_from_db()
        self.assertEqual(brand.name, "New Brand")

    def test_admin_can_delete_brand(self):
        """An admin can delete a brand."""
        _login(self.client)
        brand = _make_brand()
        url = reverse("api:catalog:admin-brand-detail", args=[brand.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Brand.objects.count(), 0)


class ProductAdminTests(APITestCase):
    """Exercises admin product CRUD."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)

    def test_anonymous_cannot_manage_products(self):
        """An unauthenticated caller cannot manage products."""
        self.client.credentials()
        response = self.client.post(
            URLS["admin_products"],
            {"name": "P", "sku": "P-1", "description": "d"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(Product.objects.count(), 0)

    def test_customer_cannot_manage_products(self):
        """A plain non-staff user cannot manage products."""
        _make_user()
        self.client.force_authenticate(user=User.objects.get(email="buyer@example.com"))
        response = self.client.post(
            URLS["admin_products"],
            {"name": "P", "sku": "P-1", "description": "d"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create_product(self):
        """An admin can create a product with auto-generated slug."""
        category = _make_category()
        brand = _make_brand()
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Coffee Maker",
                "sku": "COF-1",
                "description": "A coffee maker.",
                "category": category.id,
                "brand": brand.id,
                "price": "45000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], "coffee-maker")

    def test_duplicate_sku_rejected(self):
        """Creating a product with an existing SKU yields a 400."""
        _make_product(sku="DUP-SKU")
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Other",
                "sku": "DUP-SKU",
                "description": "d",
                "price": "45000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Product.objects.count(), 1)

    def test_duplicate_slug_rejected(self):
        """Creating a product with an existing slug yields a 400."""
        _make_product(slug="dup-slug")
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Other",
                "slug": "dup-slug",
                "sku": "SKU-9",
                "description": "d",
                "price": "45000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_overlong_slug_rejected(self):
        """A slug longer than the model field returns a 400, not a server error."""
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Long Slug Product",
                "slug": "a" * 51,
                "sku": "LONG-SLUG-1",
                "description": "d",
                "price": "45000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Product.objects.count(), 0)

    def test_overlong_sku_rejected(self):
        """A SKU longer than the model field returns a 400, not a server error."""
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Long SKU Product",
                "slug": "long-sku-product",
                "sku": "S" * 101,
                "description": "d",
                "price": "45000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Product.objects.count(), 0)

    def test_admin_can_update_product(self):
        """An admin can update a product."""
        product = _make_product()
        url = reverse("api:catalog:admin-product-detail", args=[product.pk])
        response = self.client.patch(url, {"name": "Updated Fridge"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        product.refresh_from_db()
        self.assertEqual(product.name, "Updated Fridge")

    def test_admin_can_delete_product(self):
        """An admin can delete a product."""
        product = _make_product()
        url = reverse("api:catalog:admin-product-detail", args=[product.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Product.objects.count(), 0)

    def test_admin_can_update_product_price(self):
        """An admin can update a product's sellable price."""
        product = _make_product()
        url = reverse("api:catalog:admin-product-detail", args=[product.pk])
        response = self.client.patch(url, {"price": "50000.00"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        product.refresh_from_db()
        self.assertEqual(str(product.price), "50000.00")

    def test_admin_can_mark_product_out_of_stock(self):
        """An admin can flag a product out of stock."""
        product = _make_product()
        url = reverse("api:catalog:admin-product-detail", args=[product.pk])
        response = self.client.patch(
            url, {"stock_status": "out_of_stock"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        product.refresh_from_db()
        self.assertEqual(product.stock_status, "out_of_stock")

    def test_admin_product_list_searches_by_name_and_sku(self):
        """Admin list search matches product names and SKUs."""
        product = _make_product(name="Coffee Maker", slug="coffee-maker", sku="COF-42")
        _make_product(name="Desk Fan", slug="desk-fan", sku="FAN-7")
        by_name = self.client.get(URLS["admin_products"], {"search": "Coffee"})
        self.assertEqual(by_name.data["count"], 1)
        self.assertEqual(by_name.data["results"][0]["id"], product.id)
        by_sku = self.client.get(URLS["admin_products"], {"search": "FAN-7"})
        self.assertEqual(by_sku.data["count"], 1)
        self.assertEqual(by_sku.data["results"][0]["sku"], "FAN-7")

    def test_admin_product_list_filters_by_fields(self):
        """Admin list filters narrow results by category, brand, status, and stock."""
        category = _make_category(name="Kitchen", slug="kitchen")
        brand = _make_brand(name="Acme", slug="acme")
        active = _make_product(
            name="Active Blender",
            slug="active-blender",
            sku="ACT-1",
            category=category,
            brand=brand,
            is_active=True,
            stock_status="in_stock",
        )
        _make_product(
            name="Retired Blender",
            slug="retired-blender",
            sku="RET-1",
            category=category,
            brand=brand,
            is_active=False,
            stock_status="out_of_stock",
        )
        by_status = self.client.get(URLS["admin_products"], {"is_active": "true"})
        self.assertEqual(by_status.data["count"], 1)
        self.assertEqual(by_status.data["results"][0]["id"], active.id)
        by_stock = self.client.get(
            URLS["admin_products"], {"stock_status": "out_of_stock"}
        )
        self.assertEqual(by_stock.data["count"], 1)
        self.assertEqual(by_stock.data["results"][0]["sku"], "RET-1")
        by_category = self.client.get(URLS["admin_products"], {"category": category.id})
        self.assertEqual(by_category.data["count"], 2)
        by_brand = self.client.get(URLS["admin_products"], {"brand": brand.id})
        self.assertEqual(by_brand.data["count"], 2)


class NestedResourceOwnershipTests(APITestCase):
    """Exercises parent-ownership validation for nested admin resources."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product_a = _make_product(name="A", slug="product-a", sku="A-1")
        self.product_b = _make_product(name="B", slug="product-b", sku="B-1")

    def test_pricing_tier_belongs_to_parent_product(self):
        """A pricing tier created under a product is owned by that product."""
        url = reverse(
            "api:catalog:admin-pricing-tier-list-create", args=[self.product_a.pk]
        )
        response = self.client.post(
            url,
            {"min_quantity": 5, "unit_price": "41000.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tier = PricingTier.objects.get()
        self.assertEqual(tier.product_id, self.product_a.pk)

    def test_pricing_tier_detail_rejects_foreign_product_scope(self):
        """A tier cannot be addressed through a product that does not own it."""
        tier = PricingTier.objects.create(
            product=self.product_b, min_quantity=5, unit_price="100.00"
        )
        url = reverse(
            "api:catalog:admin-pricing-tier-detail",
            args=[self.product_a.pk, tier.pk],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(PricingTier.objects.count(), 1)

    def test_related_product_rejects_self_reference(self):
        """A product cannot be related to itself."""
        url = reverse(
            "api:catalog:admin-related-product-list-create",
            args=[self.product_a.pk],
        )
        response = self.client.post(
            url,
            {
                "related_product": self.product_a.id,
                "relation_type": "alternative",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(RelatedProduct.objects.count(), 0)

    def test_related_product_cross_product_ok(self):
        """A related-product link to a different product succeeds."""
        url = reverse(
            "api:catalog:admin-related-product-list-create",
            args=[self.product_a.pk],
        )
        response = self.client.post(
            url,
            {
                "related_product": self.product_b.id,
                "relation_type": "alternative",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        link = RelatedProduct.objects.get()
        self.assertEqual(link.product_id, self.product_a.pk)
        self.assertEqual(link.related_product_id, self.product_b.pk)


class FacetDefinitionCRUDTests(APITestCase):
    """Exercises admin FacetDefinition CRUD and validation."""

    def setUp(self):
        cache.clear()
        _make_admin()

    def test_anonymous_cannot_manage_facets(self):
        """An unauthenticated caller cannot manage facet definitions."""
        response = self.client.get(URLS["admin_facets"])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_manage_facets(self):
        """A plain non-staff user cannot manage facet definitions."""
        _make_user()
        self.client.force_authenticate(user=User.objects.get(email="buyer@example.com"))
        response = self.client.get(URLS["admin_facets"])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create_valid_facet(self):
        """An admin can create a valid JSON-source facet with a key."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_facets"],
            {
                "name": "Energy Rating",
                "key": "energy_rating",
                "source_field": "product_specs",
                "facet_type": "choice",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(FacetDefinition.objects.count(), 1)

    def test_json_source_requires_key(self):
        """A JSON-source facet without a key is rejected with a 400."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_facets"],
            {
                "name": "Broken",
                "key": "",
                "source_field": "product_specs",
                "facet_type": "choice",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FacetDefinition.objects.count(), 0)

    def test_field_source_requires_field_name(self):
        """A relational-source facet without a field_name is rejected."""
        _login(self.client)
        response = self.client.post(
            URLS["admin_facets"],
            {
                "name": "Broken",
                "field_name": "",
                "source_field": "product_field",
                "facet_type": "choice",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FacetDefinition.objects.count(), 0)

    def test_non_facetable_field_name_rejected(self):
        """A facet over a non-allowlisted model column is rejected.

        Money, timestamps, and other non-facet columns must never become a
        facet source  -  each one would ship unbounded aggregation over data
        storefronts do not filter by.
        """
        _login(self.client)
        for field_name in ("created_at", "review_count"):
            response = self.client.post(
                URLS["admin_facets"],
                {
                    "name": "Broken",
                    "field_name": field_name,
                    "source_field": "product_field",
                    "facet_type": "choice",
                },
                format="json",
            )
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, field_name
            )
        self.assertEqual(FacetDefinition.objects.count(), 0)

    def test_admin_can_update_facet(self):
        """An admin can update a facet definition."""
        _login(self.client)
        facet = FacetDefinition.objects.create(
            name="Capacity",
            key="capacity",
            source_field="product_specs",
            facet_type="choice",
            is_active=True,
        )
        url = reverse("api:catalog:admin-facet-detail", args=[facet.pk])
        response = self.client.patch(url, {"facet_type": "range"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        facet.refresh_from_db()
        self.assertEqual(facet.facet_type, "range")

    def test_admin_can_delete_facet(self):
        """An admin can delete a facet definition."""
        _login(self.client)
        facet = FacetDefinition.objects.create(
            name="Capacity",
            key="capacity",
            source_field="product_specs",
            facet_type="choice",
        )
        url = reverse("api:catalog:admin-facet-detail", args=[facet.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(FacetDefinition.objects.count(), 0)


class AtomicCreationTests(APITestCase):
    """Verifies multi-model product creation is atomic."""

    def setUp(self):
        cache.clear()

    def test_create_product_rolls_back_on_child_failure(self):
        """A failing child create rolls back the product and siblings.

        Uses the service directly with a tier missing its unit price to
        force a mid-way failure and confirm nothing is left orphaned.
        """
        from django.db import IntegrityError

        from apps.catalog.services import create_product

        with self.assertRaises(IntegrityError):
            create_product(
                name="Atomic Product",
                sku="ATOMIC-1",
                description="d",
                price="100.00",
                pricing_tiers=[
                    {"min_quantity": 1, "unit_price": "100.00"},
                    {"min_quantity": 5},
                ],
            )
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(PricingTier.objects.count(), 0)

    def test_create_product_with_children_succeeds(self):
        """A successful multi-child create makes the product and children."""
        from apps.catalog.services import create_product

        product = create_product(
            name="Complete Product",
            sku="COMPLETE-1",
            description="d",
            price="100.00",
            pricing_tiers=[
                {"min_quantity": 1, "unit_price": "100.00"},
                {"min_quantity": 5, "unit_price": "90.00"},
            ],
        )
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(product.pricing_tiers.count(), 2)


class ImageUploadTests(APITestCase):
    """Exercises image upload validation on ProductImage."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product = _make_product()

    def _png(self, size=(1, 1), color=(255, 0, 0), name="test.png"):
        """Return an in-memory PNG file wrapped as an upload with a filename."""
        buffer = io.BytesIO()
        Image.new("RGBA", size, color).save(buffer, format="PNG")
        buffer.seek(0)
        return SimpleUploadedFile(name, buffer.read(), content_type="image/png")

    def test_valid_png_upload_succeeds(self):
        """A valid PNG upload to a product succeeds."""
        url = reverse(
            "api:catalog:admin-product-image-list-create", args=[self.product.pk]
        )
        response = self.client.post(url, {"image": self._png()}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ProductImage.objects.count(), 1)

    def test_text_file_rejected(self):
        """A non-image file is rejected by the image validator."""
        from django.core.exceptions import ValidationError

        from apps.catalog.validators import validate_image_upload

        class FakeFile:
            size = 1024
            name = "fake.txt"

            def read(self):
                return b"not an image"

        with self.assertRaises(ValidationError):
            validate_image_upload(FakeFile())


class SlugServiceTests(APITestCase):
    """Exercises the unique-slug generation helper."""

    def test_slug_generation_handles_duplicates(self):
        """Repeated names get suffixed unique slugs."""
        from apps.catalog.services import generate_unique_slug

        Category.objects.create(name="Fridge", slug="fridge")
        slug2 = generate_unique_slug(Category(name="Fridge"), "Fridge")
        self.assertEqual(slug2, "fridge-1")

    def test_generated_slug_respects_field_max_length(self):
        """Long names are truncated before the uniqueness suffix is applied."""
        from apps.catalog.services import generate_unique_slug

        name = "Signature SG-B219 2-in-1 Countertop Blender & Grinder Mill 500W"
        max_length = Product._meta.get_field("slug").max_length
        first = generate_unique_slug(Product(name=name), name)
        Product.objects.create(
            name=name,
            slug=first,
            sku="LONG-SLUG-1",
            price="4500.00",
            description="d",
        )
        second = generate_unique_slug(Product(name=name), name)

        self.assertLessEqual(len(first), max_length)
        self.assertLessEqual(len(second), max_length)
        self.assertNotEqual(first, second)


class ModelValidationTests(APITestCase):
    """Exercises model-level clean validation on FacetDefinition."""

    def test_facet_clean_rejects_product_specs_without_key(self):
        """Model clean() rejects a JSON-source facet missing its key."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        facet = FacetDefinition(
            name="Bad",
            key="",
            source_field="product_specs",
            facet_type="choice",
        )
        with self.assertRaises(DjangoValidationError):
            facet.clean()

    def test_facet_clean_rejects_product_field_without_field_name(self):
        """Model clean() rejects a relational-source facet missing field_name."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        facet = FacetDefinition(
            name="Bad",
            field_name="",
            source_field="product_field",
            facet_type="choice",
        )
        with self.assertRaises(DjangoValidationError):
            facet.clean()


class NumericAndRangeFacetTests(APITestCase):
    """Exercises numeric JSON facet matching and range facet filtering."""

    def setUp(self):
        cache.clear()
        FacetDefinition.objects.create(
            name="Capacity",
            key="capacity",
            source_field="product_specs",
            facet_type="choice",
            is_active=True,
        )
        FacetDefinition.objects.create(
            name="Loading",
            key="load_kg",
            source_field="product_specs",
            facet_type="range",
            is_active=True,
        )
        FacetDefinition.objects.create(
            name="Wattage",
            field_name="wattage",
            source_field="product_field",
            facet_type="range",
            is_active=True,
        )
        FacetDefinition.objects.create(
            name="Price",
            field_name="price",
            source_field="product_field",
            facet_type="range",
            is_active=True,
        )
        _make_product(
            name="Small Fridge",
            slug="num-fridge-small",
            sku="NUM-200",
            specs={"capacity": 200, "load_kg": 50},
            price="30000.00",
        )
        Product.objects.filter(sku="NUM-200").update(wattage="500.00")
        _make_product(
            name="Big Fridge",
            slug="num-fridge-big",
            sku="NUM-400",
            specs={"capacity": 400, "load_kg": 120},
            price="45000.00",
        )
        Product.objects.filter(sku="NUM-400").update(wattage="1500.00")

    def test_numeric_query_param_matches_stored_int(self):
        """A numeric query param matches an integer JSONB value."""
        response = self.client.get(URLS["products"], {"capacity": "200"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "num-fridge-small")

    def test_numeric_choice_facet_counts(self):
        """Integer JSONB values appear as stringified counts."""
        response = self.client.get(URLS["products"])
        self.assertEqual(response.data["facets"]["Capacity"]["200"], 1)
        self.assertEqual(response.data["facets"]["Capacity"]["400"], 1)

    def test_product_field_range_filter(self):
        """A range facet on a direct product field filters with min/max."""
        response = self.client.get(
            URLS["products"], {"wattage_min": "100", "wattage_max": "1000"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "num-fridge-small")

    def test_price_range_filter(self):
        """A range facet on the product price filters directly."""
        response = self.client.get(
            URLS["products"], {"price_min": "30000", "price_max": "40000"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "num-fridge-small")

    def test_json_range_filter(self):
        """A range filter over JSONB keys narrows products."""
        response = self.client.get(
            URLS["products"], {"load_kg_min": "30", "load_kg_max": "80"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_range_facets_report_min_max_bounds(self):
        """Range facets expose min/max bounds instead of per-value counts."""
        response = self.client.get(URLS["products"])
        self.assertEqual(
            response.data["facets"]["Wattage"], {"min": "500.00", "max": "1500.00"}
        )
        self.assertEqual(
            response.data["facets"]["Price"], {"min": "30000.00", "max": "45000.00"}
        )
        self.assertEqual(
            response.data["facets"]["Loading"], {"min": "50", "max": "120"}
        )


class PrimaryImageTests(APITestCase):
    """Exercises single-primary enforcement on image creation and update."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product = _make_product()

    def _create_image(self, is_primary):
        """Create an image via the admin endpoint and return its id."""
        url = reverse(
            "api:catalog:admin-product-image-list-create", args=[self.product.pk]
        )
        response = self.client.post(
            url,
            {"image": _png_upload(), "is_primary": is_primary},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data["id"]

    def test_second_primary_demotes_the_first(self):
        """Promoting a new primary image demotes the previous one."""
        self._create_image(is_primary=True)
        second_id = self._create_image(is_primary=True)

        images = ProductImage.objects.filter(product=self.product)
        self.assertEqual(images.count(), 2)
        self.assertEqual(images.filter(is_primary=True).count(), 1)
        self.assertTrue(images.get(pk=second_id).is_primary)

    def test_update_promotion_demotes_other_primary(self):
        """PATCHing is_primary on an image demotes the existing primary."""
        primary_id = self._create_image(is_primary=True)
        secondary_id = self._create_image(is_primary=False)

        url = reverse(
            "api:catalog:admin-product-image-detail",
            args=[self.product.pk, secondary_id],
        )
        response = self.client.patch(url, {"is_primary": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        images = ProductImage.objects.filter(product=self.product)
        self.assertEqual(images.filter(is_primary=True).count(), 1)
        self.assertFalse(images.get(pk=primary_id).is_primary)
        self.assertTrue(images.get(pk=secondary_id).is_primary)

    def test_create_product_collapses_multiple_primaries(self):
        """The create-product service keeps only the first primary image."""
        from apps.catalog.services import create_product

        product = create_product(
            name="Multi Image",
            slug="multi-image",
            sku="MULTI-IMG-1",
            description="d",
            price="10000.00",
            images=[
                {"image": _png_upload(name="one.png"), "is_primary": True},
                {"image": _png_upload(name="two.png"), "is_primary": True},
            ],
        )
        self.assertEqual(ProductImage.objects.filter(product=product).count(), 2)
        self.assertEqual(
            ProductImage.objects.filter(product=product, is_primary=True).count(), 1
        )


class UploadValidatorTests(APITestCase):
    """Exercises content/size validation on every catalog upload path."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)

    def test_category_image_rejects_non_image(self):
        """A non-image upload to a category is rejected."""
        response = self.client.post(
            URLS["admin_categories"],
            {
                "name": "Cookers",
                "image": SimpleUploadedFile(
                    "logo.txt", b"not an image", content_type="text/plain"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_brand_logo_rejects_non_image(self):
        """A non-image upload as a brand logo is rejected."""
        response = self.client.post(
            URLS["admin_brands"],
            {
                "name": "LG",
                "logo": SimpleUploadedFile(
                    "logo.txt", b"not an image", content_type="text/plain"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pdf_fields_reject_non_pdf(self):
        """A non-PDF upload to a product document field is rejected."""
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Manual Product",
                "sku": "MANUAL-1",
                "description": "d",
                "price": "10000.00",
                "manual_pdf": SimpleUploadedFile(
                    "manual.txt", b"not a pdf", content_type="text/plain"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_pdf_accepted(self):
        """A well-formed PDF upload to a product document field succeeds."""
        response = self.client.post(
            URLS["admin_products"],
            {
                "name": "Manual Product",
                "sku": "MANUAL-2",
                "description": "d",
                "price": "10000.00",
                "manual_pdf": _pdf_upload(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_image_validator_leaves_stream_rewound(self):
        """Image validation rewinds the stream the storage save reads."""
        from apps.catalog.validators import validate_image_upload

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (0, 0, 255)).save(buffer, format="PNG")
        uploaded = SimpleUploadedFile(
            "photo.png", buffer.getvalue(), content_type="image/png"
        )
        validate_image_upload(uploaded)
        self.assertEqual(uploaded.tell(), 0)

    def test_pdf_validator_leaves_stream_rewound(self):
        """PDF validation rewinds the stream the storage save reads."""
        from apps.catalog.validators import validate_pdf_upload

        uploaded = SimpleUploadedFile(
            "manual.pdf", b"%PDF-1.4 rest of file", content_type="application/pdf"
        )
        validate_pdf_upload(uploaded)
        self.assertEqual(uploaded.tell(), 0)


class ImageProcessingTests(APITestCase):
    """Exercises responsive variant generation for product images."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product = _make_product()

    def test_upload_generates_responsive_variants(self):
        """A large upload is processed into width/format variants."""
        from apps.catalog.images import PROCESSED_IMAGE_WIDTHS, generate_variants

        url = reverse(
            "api:catalog:admin-product-image-list-create", args=[self.product.pk]
        )
        response = self.client.post(
            url, {"image": _png_upload(1600, 1200)}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        image = ProductImage.objects.get()
        formats = {source["format"] for source in image.image_sources}
        widths = {source["width"] for source in image.image_sources}
        self.assertEqual(widths, set(PROCESSED_IMAGE_WIDTHS))
        self.assertIn("webp", formats)
        for source in image.image_sources:
            self.assertTrue(source["url"].startswith("/media/"))
            self.assertEqual(
                source["height"],
                round(1200 * source["width"] / 1600),
            )
        self.assertEqual(
            image.image_sources,
            generate_variants(image.image.name),
        )

        detail_url = reverse(
            "api:catalog:admin-product-image-detail",
            args=[self.product.pk, image.pk],
        )
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertTrue(detail.data["default_image"].endswith("--640w.webp"))
        self.assertTrue(all("--" in s["url"] for s in detail.data["srcset"]))
        self.assertTrue(all("height" in source for source in detail.data["srcset"]))

    def test_small_upload_is_not_upscaled(self):
        """Only widths no larger than the source are generated."""
        url = reverse(
            "api:catalog:admin-product-image-list-create", args=[self.product.pk]
        )
        response = self.client.post(
            url,
            {"image": _png_upload(400, 800)},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        image = ProductImage.objects.get()
        self.assertEqual({source["width"] for source in image.image_sources}, {320})
        self.assertTrue(all(source["height"] == 640 for source in image.image_sources))

    def test_forced_regeneration_is_idempotent(self):
        """Forced runs refresh metadata without duplicating output files."""
        from apps.catalog.tasks import generate_product_image_variants

        product_image = ProductImage.objects.create(
            product=self.product,
            image=_png_upload(1600, 1200),
            image_sources=[
                {
                    "width": 640,
                    "format": "webp",
                    "url": "/media/legacy--640w.webp",
                }
            ],
        )

        first_count = generate_product_image_variants.run(
            product_image.pk,
            product_image.image.name,
            force=True,
        )
        product_image.refresh_from_db()
        first_sources = product_image.image_sources
        second_count = generate_product_image_variants.run(
            product_image.pk,
            product_image.image.name,
            force=True,
        )
        product_image.refresh_from_db()

        self.assertEqual(first_count, second_count)
        self.assertEqual(first_sources, product_image.image_sources)
        self.assertTrue(
            all("height" in source for source in product_image.image_sources)
        )

    def test_delete_removes_original_and_current_variants(self):
        """Deleting an image clears its original and fingerprinted variants."""
        from django.core.files.storage import default_storage

        from apps.catalog.images import delete_image_files, generate_variants

        product_image = ProductImage.objects.create(
            product=self.product,
            image=_png_upload(1600, 1200),
        )
        variants = generate_variants(product_image.image.name)
        self.assertTrue(variants)
        variant_names = [source["url"].split("/media/", 1)[1] for source in variants]
        self.assertTrue(all(default_storage.exists(name) for name in variant_names))

        delete_image_files(product_image.image.name)

        self.assertFalse(default_storage.exists(product_image.image.name))
        self.assertFalse(any(default_storage.exists(name) for name in variant_names))

    @mock.patch("apps.catalog.tasks.generate_product_image_variants.delay")
    def test_regeneration_command_queues_existing_images(self, enqueue):
        """The command queues forced regeneration without changing image rows."""
        product_image = ProductImage.objects.create(
            product=self.product,
            image=_png_upload(),
            image_sources=[
                {
                    "width": 640,
                    "height": 480,
                    "format": "webp",
                    "url": "/media/old--640w.webp",
                }
            ],
        )

        call_command(
            "regenerate_product_image_variants",
            "--product-id",
            str(self.product.pk),
        )

        enqueue.assert_called_once_with(
            product_image.pk,
            product_image.image.name,
            force=True,
        )

    @mock.patch("apps.catalog.tasks.generate_product_image_variants.delay")
    def test_regeneration_command_dry_run_does_not_enqueue(self, enqueue):
        """A dry run reports candidates without sending Celery tasks."""
        ProductImage.objects.create(
            product=self.product,
            image=_png_upload(),
        )

        output = io.StringIO()
        call_command("regenerate_product_image_variants", "--dry-run", stdout=output)

        enqueue.assert_not_called()
        self.assertIn(
            "Would queue variant regeneration for 1 image(s)", output.getvalue()
        )

    def test_primary_image_in_list_uses_processed_variant(self):
        """The public list serves the processed variant, not the original."""
        url = reverse(
            "api:catalog:admin-product-image-list-create", args=[self.product.pk]
        )
        self.client.post(
            url,
            {"image": _png_upload(1600, 1200), "is_primary": True},
            format="multipart",
        )

        response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        primary_url = response.data["results"][0]["primary_image"]
        self.assertIn("--640w.webp", primary_url)
        self.assertNotIn("photo.png", primary_url)


class NestedResourceUpdateTests(APITestCase):
    """Exercises the newly added update endpoints for nested resources."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product_a = _make_product(name="A", slug="nested-a", sku="NA-1")
        self.product_b = _make_product(name="B", slug="nested-b", sku="NB-1")

    def test_image_update(self):
        """An image's alt text and ordering can be updated in place."""
        url = reverse(
            "api:catalog:admin-product-image-list-create", args=[self.product_a.pk]
        )
        created = self.client.post(
            url, {"image": _png_upload(), "sort_order": 5}, format="multipart"
        )
        detail_url = reverse(
            "api:catalog:admin-product-image-detail",
            args=[self.product_a.pk, created.data["id"]],
        )
        response = self.client.patch(
            detail_url, {"alt_text": "Hero shot", "sort_order": 1}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        image = ProductImage.objects.get(pk=created.data["id"])
        self.assertEqual(image.alt_text, "Hero shot")
        self.assertEqual(image.sort_order, 1)

    def test_pricing_tier_update(self):
        """A pricing tier's price can be updated in place."""
        tier = PricingTier.objects.create(
            product=self.product_a, min_quantity=5, unit_price="41000.00"
        )
        url = reverse(
            "api:catalog:admin-pricing-tier-detail",
            args=[self.product_a.pk, tier.pk],
        )
        response = self.client.patch(url, {"unit_price": "39999.99"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tier.refresh_from_db()
        self.assertEqual(tier.unit_price, Decimal("39999.99"))

    def test_pricing_tier_update_rejects_foreign_product_scope(self):
        """A tier cannot be updated through a product that does not own it."""
        tier = PricingTier.objects.create(
            product=self.product_a, min_quantity=5, unit_price="41000.00"
        )
        url = reverse(
            "api:catalog:admin-pricing-tier-detail",
            args=[self.product_b.pk, tier.pk],
        )
        response = self.client.patch(url, {"unit_price": "1.00"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        tier.refresh_from_db()
        self.assertEqual(tier.unit_price, Decimal("41000.00"))

    def test_related_product_update(self):
        """A related-product link's type and order can be updated."""
        link = RelatedProduct.objects.create(
            product=self.product_a,
            related_product=self.product_b,
            relation_type="alternative",
        )
        url = reverse(
            "api:catalog:admin-related-product-detail",
            args=[self.product_a.pk, link.pk],
        )
        response = self.client.patch(
            url, {"relation_type": "accessory", "sort_order": 3}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        link.refresh_from_db()
        self.assertEqual(link.relation_type, "accessory")
        self.assertEqual(link.sort_order, 3)


class CategoryIntegrityTests(APITestCase):
    """Exercises category-tree and related-link integrity rules."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)

    def test_category_cannot_be_its_own_parent(self):
        """Setting a category's parent to itself returns a 400."""
        category = Category.objects.create(name="Root", slug="root-cat")
        url = reverse("api:catalog:admin-category-detail", args=[category.pk])
        response = self.client.patch(url, {"parent": category.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_category_parent_cycle_rejected(self):
        """Reparenting a category into its own descendant subtree is rejected."""
        root = Category.objects.create(name="Root", slug="cycle-root")
        middle = Category.objects.create(name="Middle", slug="cycle-mid", parent=root)
        leaf = Category.objects.create(name="Leaf", slug="cycle-leaf", parent=middle)

        url = reverse("api:catalog:admin-category-detail", args=[root.pk])
        response = self.client.patch(url, {"parent": leaf.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_legitimate_reparent_succeeds(self):
        """Moving a category under a different root still works."""
        root = Category.objects.create(name="Root", slug="legit-root")
        child = Category.objects.create(name="Child", slug="legit-child", parent=root)
        new_root = Category.objects.create(name="New Root", slug="legit-new")

        url = reverse("api:catalog:admin-category-detail", args=[child.pk])
        response = self.client.patch(url, {"parent": new_root.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        child.refresh_from_db()
        self.assertEqual(child.parent_id, new_root.pk)

    def test_duplicate_related_product_rejected(self):
        """Linking the same product pair twice returns a 400."""
        product_a = _make_product(name="A", slug="dup-a", sku="DUP-A")
        product_b = _make_product(name="B", slug="dup-b", sku="DUP-B")
        url = reverse(
            "api:catalog:admin-related-product-list-create", args=[product_a.pk]
        )
        payload = {
            "related_product": product_b.pk,
            "relation_type": "alternative",
        }
        first = self.client.post(url, payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.client.post(url, payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)


class ProductFAQTests(APITestCase):
    """Exercises product-specific FAQ reads, replacement, and validation."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product = _make_product()
        self.url = reverse("api:catalog:admin-product-faq", args=[self.product.pk])

    def _faq_payload(self, *rows):
        """Build a replace payload from ``(question, answer)`` pairs."""
        return {
            "faqs": [
                {"question": question, "answer": answer} for question, answer in rows
            ]
        }

    def test_public_detail_includes_ordered_faqs(self):
        """Public product detail returns FAQs in position order."""
        ProductFAQ.objects.create(
            product=self.product,
            question="Second question?",
            answer="The second answer is here.",
            position=1,
        )
        ProductFAQ.objects.create(
            product=self.product,
            question="First question?",
            answer="The first answer is here.",
            position=0,
        )
        detail_url = reverse("api:catalog:product-detail", args=[self.product.slug])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [faq["question"] for faq in response.data["faqs"]],
            ["First question?", "Second question?"],
        )
        self.assertEqual(set(response.data["faqs"][0]), {"id", "question", "answer"})

    def test_public_detail_returns_empty_faq_list(self):
        """A product without FAQs still exposes an empty list."""
        detail_url = reverse("api:catalog:product-detail", args=[self.product.slug])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["faqs"], [])

    def test_anonymous_cannot_read_staff_faqs(self):
        """The staff FAQ endpoint requires authentication."""
        self.client.credentials()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_read_staff_faqs(self):
        """A non-staff user cannot read the staff FAQ endpoint."""
        _make_user()
        self.client.force_authenticate(user=User.objects.get(email="buyer@example.com"))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_read_faqs(self):
        """Staff receive the ordered FAQ list with positions."""
        ProductFAQ.objects.create(
            product=self.product,
            question="Is delivery free?",
            answer="Delivery is free within Nairobi.",
            position=0,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["faqs"][0]["position"], 0)
        self.assertEqual(response.data["faqs"][0]["question"], "Is delivery free?")

    def test_replace_faqs_normalizes_and_orders_rows(self):
        """Replacing FAQs trims text, preserves paragraphs, and sets positions."""
        response = self.client.put(
            self.url,
            self._faq_payload(
                ("  Does it come with a warranty?  ", "  Yes.\n\nTwo years.  "),
                ("Can I pay on delivery?", "Yes, within Nairobi."),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([faq["position"] for faq in response.data["faqs"]], [0, 1])
        self.assertEqual(
            response.data["faqs"][0]["question"], "Does it come with a warranty?"
        )
        self.assertEqual(response.data["faqs"][0]["answer"], "Yes.\n\nTwo years.")

    def test_replace_faqs_preserves_ids_when_content_and_order_change(self):
        """Existing FAQ IDs survive edits and reordering so anchors stay stable."""
        first = ProductFAQ.objects.create(
            product=self.product,
            question="First question?",
            answer="The first answer is long enough.",
            position=0,
        )
        second = ProductFAQ.objects.create(
            product=self.product,
            question="Second question?",
            answer="The second answer is long enough.",
            position=1,
        )
        response = self.client.put(
            self.url,
            {
                "faqs": [
                    {
                        "id": second.pk,
                        "question": "Second question updated?",
                        "answer": "The second answer was updated.",
                    },
                    {
                        "id": first.pk,
                        "question": "First question updated?",
                        "answer": "The first answer was updated.",
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {faq["id"]: faq for faq in response.data["faqs"]}
        self.assertEqual(set(rows), {first.pk, second.pk})
        self.assertEqual(rows[first.pk]["question"], "First question updated?")
        self.assertEqual(rows[first.pk]["position"], 1)
        self.assertEqual(rows[second.pk]["position"], 0)
        self.assertEqual(ProductFAQ.objects.count(), 2)

    def test_replace_faqs_creates_rows_without_ids(self):
        """Rows without an id are inserted without disturbing existing ids."""
        existing = ProductFAQ.objects.create(
            product=self.product,
            question="Existing question?",
            answer="The existing answer is long enough.",
            position=0,
        )
        response = self.client.put(
            self.url,
            {
                "faqs": [
                    {
                        "id": existing.pk,
                        "question": "Existing question?",
                        "answer": "The existing answer is long enough.",
                    },
                    {
                        "question": "New question?",
                        "answer": "The new answer is long enough.",
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [faq["id"] for faq in response.data["faqs"]]
        self.assertIn(existing.pk, ids)
        self.assertEqual(len(set(ids)), 2)

    def test_replace_faqs_rejects_id_from_another_product(self):
        """A row id cannot be used to move or edit another product's FAQ."""
        other_product = _make_product(name="Other fridge")
        other_faq = ProductFAQ.objects.create(
            product=other_product,
            question="Other product question?",
            answer="The other product answer is long enough.",
            position=0,
        )
        response = self.client.put(
            self.url,
            {
                "faqs": [
                    {
                        "id": other_faq.pk,
                        "question": "Attempted question?",
                        "answer": "Attempted answer is long enough.",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("non_field_errors", response.data["errors"]["faqs"]["0"])
        other_faq.refresh_from_db()
        self.assertEqual(other_faq.question, "Other product question?")

    def test_replace_faqs_drops_blank_rows(self):
        """Rows with both fields empty are ignored instead of saved."""
        response = self.client.put(
            self.url,
            {
                "faqs": [
                    {"question": "", "answer": ""},
                    {"question": "Is it available?", "answer": "Yes, it is in stock."},
                    {"question": "   ", "answer": "   "},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(ProductFAQ.objects.count(), 1)
        self.assertEqual(response.data["faqs"][0]["position"], 0)

    def test_replace_faqs_rejects_half_filled_row(self):
        """A question without an answer is rejected with a field error."""
        response = self.client.put(
            self.url,
            {"faqs": [{"question": "Is it available?", "answer": ""}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("answer", response.data["errors"]["faqs"]["0"])

    def test_replace_faqs_rejects_duplicate_question_case_insensitively(self):
        """The same question cannot appear twice with different casing."""
        response = self.client.put(
            self.url,
            self._faq_payload(
                ("Does it have a warranty?", "Yes, it has a warranty."),
                ("does it have a WARRANTY?", "Yes, it has a warranty."),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("question", response.data["errors"]["faqs"]["1"])

    def test_replace_faqs_rejects_html(self):
        """HTML in questions or answers is rejected."""
        response = self.client.put(
            self.url,
            self._faq_payload(
                ("Is it <b>new</b>?", "Yes, it is brand new."),
                ("Can I return it?", "Yes <script>alert(1)</script>."),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("question", response.data["errors"]["faqs"]["0"])
        self.assertIn("answer", response.data["errors"]["faqs"]["1"])

    def test_replace_faqs_rejects_short_content(self):
        """Questions and answers below the minimum length are rejected."""
        response = self.client.put(
            self.url,
            self._faq_payload(("Hi", "Too short")),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("question", response.data["errors"]["faqs"]["0"])
        self.assertIn("answer", response.data["errors"]["faqs"]["0"])

    def test_replace_faqs_rejects_more_than_twenty(self):
        """The twenty-first non-empty row is rejected."""
        rows = [
            (f"Question number {index}?", f"Answer number {index} is long enough.")
            for index in range(21)
        ]
        response = self.client.put(self.url, self._faq_payload(*rows), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("non_field_errors", response.data["errors"]["faqs"]["20"])

    def test_invalid_replacement_keeps_existing_faqs(self):
        """A rejected replacement does not delete the current FAQ list."""
        ProductFAQ.objects.create(
            product=self.product,
            question="Existing question?",
            answer="This row must survive a rejected write.",
            position=0,
        )
        response = self.client.put(
            self.url,
            self._faq_payload(("New question?", "")),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ProductFAQ.objects.count(), 1)
        self.assertEqual(ProductFAQ.objects.get().question, "Existing question?")

    def test_replacement_bumps_product_list_generation(self):
        """A successful replacement invalidates catalog caches."""
        before = catalog_cache.get_product_list_generation()
        response = self.client.put(
            self.url,
            self._faq_payload(("Is it in stock?", "Yes, it is in stock.")),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(catalog_cache.get_product_list_generation(), before)

    def test_model_clean_normalizes_and_rejects_invalid_rows(self):
        """Admin/full_clean applies the same text rules as the API."""
        faq = ProductFAQ(
            product=self.product,
            question="  What   is this?  ",
            answer="  First line.\n\n\nSecond line.  ",
            position=0,
        )
        faq.full_clean()
        self.assertEqual(faq.question, "What is this?")
        self.assertEqual(faq.answer, "First line.\n\nSecond line.")

        faq.question = "Hi"
        with self.assertRaises(ValidationError):
            faq.full_clean()

        faq.question = "Is it <b>new</b>?"
        with self.assertRaises(ValidationError):
            faq.full_clean()

    def test_model_clean_rejects_duplicate_question_and_position(self):
        """Admin validation catches conflicts before the database does."""
        ProductFAQ.objects.create(
            product=self.product,
            question="Existing question?",
            answer="The existing answer is long enough.",
            position=0,
        )
        faq = ProductFAQ(
            product=self.product,
            question="existing QUESTION?",
            answer="Another answer is long enough.",
            position=1,
        )
        with self.assertRaises(ValidationError):
            faq.full_clean()

        faq.question = "Different question?"
        faq.position = 0
        with self.assertRaises(ValidationError):
            faq.full_clean()

    def test_deleting_product_cascades_faqs(self):
        """Deleting a product removes its FAQ rows."""
        ProductFAQ.objects.create(
            product=self.product,
            question="Will this remain?",
            answer="No, it is removed with the product.",
            position=0,
        )
        self.product.delete()
        self.assertEqual(ProductFAQ.objects.count(), 0)


class StorefrontRevalidationTests(APITestCase):
    """Exercises the storefront webhook used after FAQ changes."""

    def setUp(self):
        cache.clear()
        self.product = _make_product()

    @override_settings(
        STOREFRONT_REVALIDATE_URL="https://shop.example.com/api/revalidate",
        STOREFRONT_REVALIDATE_SECRET="shared-secret",
    )
    @mock.patch("apps.catalog.tasks.urllib.request.urlopen")
    def test_revalidation_posts_slug_and_secret(self, urlopen):
        """The task sends the product slug and shared secret to the storefront."""
        response = mock.MagicMock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response

        from apps.catalog.tasks import revalidate_storefront_product

        self.assertTrue(revalidate_storefront_product(self.product.pk))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://shop.example.com/api/revalidate")
        self.assertEqual(json.loads(request.data), {"slug": self.product.slug})
        self.assertEqual(request.get_header("X-revalidate-secret"), "shared-secret")

    @override_settings(STOREFRONT_REVALIDATE_URL="", STOREFRONT_REVALIDATE_SECRET="")
    @mock.patch("apps.catalog.tasks.urllib.request.urlopen")
    def test_revalidation_is_disabled_without_config(self, urlopen):
        """No request is made when the webhook is not configured."""
        from apps.catalog.tasks import revalidate_storefront_product

        self.assertFalse(revalidate_storefront_product(self.product.pk))
        urlopen.assert_not_called()


class ProductDetailQueryTests(APITestCase):
    """Binds the product-detail endpoint to a fixed query count."""

    def test_product_detail_is_query_bound(self):
        """Detail serialization does not fire per-relation count queries."""
        product = _make_product()
        ProductImage.objects.create(
            product=product, image=_png_upload(), is_primary=True
        )
        url = reverse("api:catalog:product-detail", args=[product.slug])
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["category"]["name"], product.category.name)
        # Product, tier prefetch, image prefetch, and FAQ prefetch.
        self.assertLessEqual(len(ctx.captured_queries), 5)


class ProductPriceThrottleTests(APITestCase):
    """The card-price endpoint has its own budget, separate from catalog reads."""

    def setUp(self):
        cache.clear()
        self.product = _make_product(
            name="Price Burst", slug="price-burst", sku="PR-BURST"
        )
        self.url = reverse("api:catalog:product-price", args=[self.product.slug])

    def test_a_listing_page_price_burst_is_not_rejected(self):
        """Six card lookups in one page load all succeed."""
        for _ in range(6):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_price_endpoint_scope_and_rate_are_configured(self):
        """The price scope is dedicated and generous while auth stays strict."""
        from rest_framework.settings import api_settings

        from apps.catalog.views import ProductPriceView

        rates = api_settings.DEFAULT_THROTTLE_RATES
        self.assertEqual(ProductPriceView.throttle_scope, "product_price")
        self.assertEqual(rates["product_price"], "300/min")
        self.assertNotEqual(rates["product_price"], rates["public_catalog"])
        self.assertEqual(rates["auth_login"], "3/min")
        self.assertEqual(rates["auth_reauth"], "10/min")
        self.assertEqual(rates["inquiry_write"], "20/min")
        self.assertEqual(rates["order_write"], "5/min")
