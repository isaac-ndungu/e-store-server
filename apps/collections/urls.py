"""URL routing for the public and admin collection endpoints."""

from django.urls import path

from apps.collections.views import (
    AdminCollectionDetailView,
    AdminCollectionListCreateView,
    AdminCollectionMembershipDetailView,
    AdminCollectionMembershipListCreateView,
    CollectionDetailView,
    CollectionListView,
)

urlpatterns = [
    path("collections/", CollectionListView.as_view(), name="collection-list"),
    # Admin CRUD. These register before the slug detail path so "admin" is not
    # captured as a collection slug.
    path(
        "collections/admin/",
        AdminCollectionListCreateView.as_view(),
        name="admin-collection-list-create",
    ),
    path(
        "collections/admin/<int:pk>/",
        AdminCollectionDetailView.as_view(),
        name="admin-collection-detail",
    ),
    path(
        "collections/admin/memberships/",
        AdminCollectionMembershipListCreateView.as_view(),
        name="admin-collection-membership-list-create",
    ),
    path(
        "collections/admin/memberships/<int:pk>/",
        AdminCollectionMembershipDetailView.as_view(),
        name="admin-collection-membership-detail",
    ),
    path(
        "collections/<slug:slug>/",
        CollectionDetailView.as_view(),
        name="collection-detail",
    ),
]
