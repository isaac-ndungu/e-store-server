"""URL routing for the content endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Public storefront reads
use slug-based resolution for pages and placement-based queries for banners;
admin CRUD paths use pk-based resolution. Only manager-role tokens may reach
the admin endpoints.
"""

from django.urls import path

from apps.content.views import (
    BannerAdminCreateView,
    BannerAdminDeleteView,
    BannerAdminDetailView,
    BannerAdminListView,
    BannerAdminUpdateView,
    BannerStorefrontView,
    ContentPageAdminCreateView,
    ContentPageAdminDeleteView,
    ContentPageAdminDetailView,
    ContentPageAdminListView,
    ContentPageAdminUpdateView,
    ContentPageStorefrontView,
)

urlpatterns = [
    path(
        "content/pages/<slug:slug>/",
        ContentPageStorefrontView.as_view(),
        name="content-page-storefront",
    ),
    path(
        "content/banners/",
        BannerStorefrontView.as_view(),
        name="banner-storefront",
    ),
    path(
        "content/admin/pages/",
        ContentPageAdminListView.as_view(),
        name="content-page-admin-list",
    ),
    path(
        "content/admin/pages/create/",
        ContentPageAdminCreateView.as_view(),
        name="content-page-admin-create",
    ),
    path(
        "content/admin/pages/<int:page_id>/",
        ContentPageAdminDetailView.as_view(),
        name="content-page-admin-detail",
    ),
    path(
        "content/admin/pages/<int:page_id>/update/",
        ContentPageAdminUpdateView.as_view(),
        name="content-page-admin-update",
    ),
    path(
        "content/admin/pages/<int:page_id>/delete/",
        ContentPageAdminDeleteView.as_view(),
        name="content-page-admin-delete",
    ),
    path(
        "content/admin/banners/",
        BannerAdminListView.as_view(),
        name="banner-admin-list",
    ),
    path(
        "content/admin/banners/create/",
        BannerAdminCreateView.as_view(),
        name="banner-admin-create",
    ),
    path(
        "content/admin/banners/<int:banner_id>/",
        BannerAdminDetailView.as_view(),
        name="banner-admin-detail",
    ),
    path(
        "content/admin/banners/<int:banner_id>/update/",
        BannerAdminUpdateView.as_view(),
        name="banner-admin-update",
    ),
    path(
        "content/admin/banners/<int:banner_id>/delete/",
        BannerAdminDeleteView.as_view(),
        name="banner-admin-delete",
    ),
]
