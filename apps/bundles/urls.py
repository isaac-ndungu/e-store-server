from django.urls import path

from apps.bundles.views import (
    AdminBundleDetailView,
    AdminBundleItemDetailView,
    AdminBundleItemListCreateView,
    AdminBundleListCreateView,
    BundleDetailView,
    BundleListView,
    BundlePriceView,
)

urlpatterns = [
    path("bundles/", BundleListView.as_view(), name="bundle-list"),
    # Admin CRUD. These register before the slug detail path so "admin" is not
    # captured as a bundle slug.
    path(
        "bundles/admin/",
        AdminBundleListCreateView.as_view(),
        name="admin-bundle-list-create",
    ),
    path(
        "bundles/admin/<int:pk>/",
        AdminBundleDetailView.as_view(),
        name="admin-bundle-detail",
    ),
    path(
        "bundles/admin/<int:bundle_pk>/items/",
        AdminBundleItemListCreateView.as_view(),
        name="admin-bundle-item-list-create",
    ),
    path(
        "bundles/admin/<int:bundle_pk>/items/<int:pk>/",
        AdminBundleItemDetailView.as_view(),
        name="admin-bundle-item-detail",
    ),
    path(
        "bundles/<slug:slug>/price/",
        BundlePriceView.as_view(),
        name="bundle-price",
    ),
    path(
        "bundles/<slug:slug>/",
        BundleDetailView.as_view(),
        name="bundle-detail",
    ),
]
