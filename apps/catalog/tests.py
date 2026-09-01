"""Tests for the catalog app.

Covers public browse endpoints (categories, brands, products, search,
filtering, faceted aggregation), admin CRUD for every model, nested-resource
parent-ownership validation, inactive-product detail 404s, atomic multi-model
creation, facet definition validation and CRUD, image upload validation, and
the security / role-based separation between anonymous, customer, and admin
callers.
"""

import io
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.text import slugify
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import (
    Brand,
    Category,
    FacetDefinition,
    PricingTier,
    Product,
    ProductImage,
    ProductVariant,
    RelatedProduct,
)

URLS = {
    "categories": reverse("api:catalog:category-list"),
    "brands": reverse("api:catalog:brand-list"),
    "products": reverse("api:catalog:product-list"),
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
    """Return a ``ProductVariant`` SKU that is unique in the test DB.

    Args:
        value (str): the base SKU.

    Returns:
        str: a unique SKU.
    """
    sku = value
    counter = 1
    while ProductVariant.objects.filter(sku=sku).exists():
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
    """Create a test product, optionally with a variant, and return it."""
    category = kwargs.get("category") or _make_category()
    brand = kwargs.get("brand") or _make_brand()
    product = Product.objects.create(
        name=kwargs.get("name", "Fridge 200L"),
        slug=kwargs.get("slug", "fridge-200l"),
        sku=kwargs.get("sku", "FRG-200"),
        description=kwargs.get("description", "A 200L refrigerator."),
        category=category,
        brand=brand,
        is_active=kwargs.get("is_active", True),
        is_discontinued=kwargs.get("is_discontinued", False),
        specs=kwargs.get("specs", {"capacity": "200L", "energy_rating": "A"}),
    )
    if kwargs.get("with_variant", False):
        _make_variant(product)
    return product


def _make_variant(product, **kwargs):
    """Create a test variant for a product and return it."""
    sku = kwargs.get("sku") or _unique_sku("FRG-200-SILVER")
    return ProductVariant.objects.create(
        product=product,
        sku=sku,
        attributes=kwargs.get("attributes", {"color": "Silver"}),
        price=kwargs.get("price", "45000.00"),
        is_active=kwargs.get("is_active", True),
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

    def test_ordering_by_price_is_not_allowed(self):
        """Price ordering is not offered on the list (variants own price)."""
        response = self.client.get(URLS["products"], {"ordering": "price"})
        # Unknown orderings are silently ignored, not an error.
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_ordering_by_name(self):
        """Products can be ordered by name."""
        response = self.client.get(URLS["products"], {"ordering": "name"})
        names = [p["name"] for p in response.data["results"]]
        self.assertEqual(names, sorted(names))

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


class ProductDetailBrowseTests(APITestCase):
    """Exercises the public product detail and pricing endpoints."""

    def setUp(self):
        cache.clear()
        self.product = _make_product(with_variant=True)

    def test_product_detail_is_public_and_includes_variants(self):
        """A product detail returns nested variants and metadata."""
        url = reverse("api:catalog:product-detail", args=[self.product.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Fridge 200L")
        self.assertEqual(len(response.data["variants"]), 1)
        self.assertEqual(response.data["variants"][0]["sku"], "FRG-200-SILVER")

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

    def test_price_endpoint_returns_variant_pricing(self):
        """The price endpoint returns per-variant base and tier pricing."""
        variant = self.product.variants.get()
        PricingTier.objects.create(
            variant=variant, min_quantity=5, unit_price="41000.00"
        )
        url = reverse("api:catalog:product-price", args=[self.product.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["product_slug"], self.product.slug)
        self.assertEqual(len(response.data["variants"]), 1)
        self.assertEqual(response.data["variants"][0]["price"], "45000.00")
        self.assertEqual(
            response.data["variants"][0]["pricing_tiers"],
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

    def test_price_endpoint_query_bound(self):
        """Variant and tier data are prefetched, not queried per variant."""
        product = _make_product(
            name="Price Bound", slug="price-bound", sku="PR-BND", with_variant=True
        )
        variant = product.variants.get()
        PricingTier.objects.create(
            variant=variant, min_quantity=5, unit_price="41000.00"
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
            source_field="variant_attributes",
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
            specs={"capacity": "200L", "energy_rating": "A"},
            with_variant=True,
        )
        _make_product(
            name="Fridge 300L",
            slug="fac-fridge-300",
            sku="FAC-300",
            specs={"capacity": "300L", "energy_rating": "B"},
            with_variant=True,
        )

    def test_response_includes_facet_counts(self):
        """The product list response carries a facets dict."""
        response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("facets", response.data)
        self.assertIn("Capacity", response.data["facets"])
        self.assertEqual(response.data["facets"]["Capacity"]["200L"], 1)
        self.assertEqual(response.data["facets"]["Capacity"]["300L"], 1)

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
            )
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(URLS["products"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Main query, image prefetch, pagination count, facet-definition
        # lookup, and one grouped aggregate per active facet (3 here).
        self.assertLessEqual(len(ctx.captured_queries), 10)
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
        """A plain customer token is rejected from category creation."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
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

    def test_admin_can_update_category(self):
        """An admin can update a category."""
        _login(self.client)
        category = _make_category(name="Old Name", slug="old-name")
        url = reverse("api:catalog:admin-category-detail", args=[category.pk])
        response = self.client.patch(url, {"name": "New Name"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        category.refresh_from_db()
        self.assertEqual(category.name, "New Name")

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
    """Exercises admin product and variant CRUD."""

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
        """A plain customer cannot manage products."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
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
            {"name": "Other", "sku": "DUP-SKU", "description": "d"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Product.objects.count(), 1)

    def test_duplicate_slug_rejected(self):
        """Creating a product with an existing slug yields a 400."""
        _make_product(slug="dup-slug")
        response = self.client.post(
            URLS["admin_products"],
            {"name": "Other", "slug": "dup-slug", "sku": "SKU-9", "description": "d"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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

    def test_admin_can_create_variant(self):
        """An admin can add a variant to a product."""
        product = _make_product()
        url = reverse(
            "api:catalog:admin-product-variant-list-create", args=[product.pk]
        )
        response = self.client.post(
            url,
            {
                "sku": "FRG-200-RED",
                "attributes": {"color": "Red"},
                "price": "46000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ProductVariant.objects.count(), 1)
        self.assertEqual(ProductVariant.objects.get().product_id, product.pk)

    def test_admin_can_update_variant(self):
        """An admin can update a variant under a product."""
        product = _make_product(with_variant=True)
        variant = product.variants.get()
        url = reverse(
            "api:catalog:admin-product-variant-detail",
            args=[product.pk, variant.pk],
        )
        response = self.client.patch(url, {"price": "50000.00"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        variant.refresh_from_db()
        self.assertEqual(str(variant.price), "50000.00")

    def test_admin_can_delete_variant(self):
        """An admin can delete a variant under a product."""
        product = _make_product(with_variant=True)
        variant = product.variants.get()
        url = reverse(
            "api:catalog:admin-product-variant-detail",
            args=[product.pk, variant.pk],
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(ProductVariant.objects.count(), 0)


class NestedResourceOwnershipTests(APITestCase):
    """Exercises parent-ownership validation for nested admin resources."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product_a = _make_product(name="A", slug="product-a", sku="A-1")
        self.product_b = _make_product(name="B", slug="product-b", sku="B-1")
        self.variant_b = _make_variant(
            self.product_b, sku="B-1-V", attributes={"color": "Blue"}
        )

    def test_pricing_tier_rejects_variant_from_other_product(self):
        """A pricing tier cannot be attached to a variant under another product."""
        url = reverse(
            "api:catalog:admin-pricing-tier-list-create", args=[self.product_a.pk]
        )
        response = self.client.post(
            url,
            {"variant": self.variant_b.id, "min_quantity": 5, "unit_price": "100.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PricingTier.objects.count(), 0)

    def test_pricing_tier_accepts_variant_of_parent_product(self):
        """A pricing tier for a variant of the parent product succeeds."""
        variant_a = _make_variant(
            self.product_a, sku="A-1-V", attributes={"color": "Red"}
        )
        url = reverse(
            "api:catalog:admin-pricing-tier-list-create", args=[self.product_a.pk]
        )
        response = self.client.post(
            url,
            {"variant": variant_a.id, "min_quantity": 5, "unit_price": "41000.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
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
        """A plain customer cannot manage facet definitions."""
        _make_user()
        _login(self.client, email="buyer@example.com", password="StrongPass123!")
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

        Uses the service directly with a duplicate variant SKU to force a
        mid-way failure and confirm nothing is left orphaned.
        """
        from django.db import IntegrityError

        from apps.catalog.services import create_product

        _make_product(
            name="Seeder",
            slug="seeder",
            sku="SEED-1",
            with_variant=True,
        )
        _make_variant(Product.objects.get(sku="SEED-1"), sku="SEED-VAR-1")
        with self.assertRaises(IntegrityError):
            create_product(
                name="Atomic Product",
                sku="ATOMIC-1",
                description="d",
                variants=[
                    {"sku": "ATO-1", "price": "100.00"},
                    {"sku": "SEED-VAR-1", "price": "200.00"},
                ],
            )
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductVariant.objects.count(), 2)

    def test_create_product_with_children_succeeds(self):
        """A successful multi-child create makes the product and children."""
        from apps.catalog.services import create_product

        product = create_product(
            name="Complete Product",
            sku="COMPLETE-1",
            description="d",
            variants=[
                {"sku": "CP-1", "price": "100.00"},
                {"sku": "CP-2", "price": "200.00"},
            ],
        )
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(product.variants.count(), 2)


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
            source_field="variant_field",
            facet_type="range",
            is_active=True,
        )
        _make_product(
            name="Small Fridge",
            slug="num-fridge-small",
            sku="NUM-200",
            specs={"capacity": 200, "load_kg": 50},
            with_variant=True,
        )
        Product.objects.filter(sku="NUM-200").update(wattage="500.00")
        _make_variant(
            Product.objects.get(sku="NUM-200"), sku="NUM-200-V", price="30000.00"
        )
        _make_product(
            name="Big Fridge",
            slug="num-fridge-big",
            sku="NUM-400",
            specs={"capacity": 400, "load_kg": 120},
            with_variant=True,
        )
        Product.objects.filter(sku="NUM-400").update(wattage="1500.00")
        _make_variant(
            Product.objects.get(sku="NUM-400"), sku="NUM-400-V", price="45000.00"
        )

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

    def test_variant_field_range_filter(self):
        """A range facet on a variant field filters through the variant join."""
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
                "manual_pdf": _pdf_upload(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ImageProcessingTests(APITestCase):
    """Exercises responsive variant generation for product images."""

    def setUp(self):
        cache.clear()
        _make_admin()
        _login(self.client)
        self.product = _make_product()

    def test_upload_generates_responsive_variants(self):
        """A large upload is processed into width/format variants."""
        from apps.catalog.images import PROCESSED_IMAGE_WIDTHS

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

        detail_url = reverse(
            "api:catalog:admin-product-image-detail",
            args=[self.product.pk, image.pk],
        )
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertTrue(detail.data["default_image"].endswith("--640w.webp"))
        self.assertTrue(all("--" in s["url"] for s in detail.data["srcset"]))

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
        self.variant_a = _make_variant(self.product_a, sku="NA-1-V")
        self.variant_b = _make_variant(
            self.product_b, sku="NB-1-V", attributes={"color": "Blue"}
        )

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
            variant=self.variant_a, min_quantity=5, unit_price="41000.00"
        )
        url = reverse(
            "api:catalog:admin-pricing-tier-detail",
            args=[self.product_a.pk, tier.pk],
        )
        response = self.client.patch(url, {"unit_price": "39999.99"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tier.refresh_from_db()
        self.assertEqual(tier.unit_price, Decimal("39999.99"))

    def test_pricing_tier_update_rejects_foreign_variant(self):
        """Moving a tier onto another product's variant is rejected."""
        tier = PricingTier.objects.create(
            variant=self.variant_a, min_quantity=5, unit_price="41000.00"
        )
        url = reverse(
            "api:catalog:admin-pricing-tier-detail",
            args=[self.product_a.pk, tier.pk],
        )
        response = self.client.patch(url, {"variant": self.variant_b.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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


class ProductDetailQueryTests(APITestCase):
    """Binds the product-detail endpoint to a fixed query count."""

    def test_product_detail_is_query_bound(self):
        """Detail serialization does not fire per-relation count queries."""
        product = _make_product(with_variant=True)
        ProductImage.objects.create(
            product=product, image=_png_upload(), is_primary=True
        )
        url = reverse("api:catalog:product-detail", args=[product.slug])
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["category"]["name"], product.category.name)
        # Product, variant prefetch (+ tier prefetch), and image prefetch.
        self.assertLessEqual(len(ctx.captured_queries), 4)
