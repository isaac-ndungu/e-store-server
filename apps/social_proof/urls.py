"""URL routing for the social proof endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. All routes are public
storefront paths. The product-scoped views live under a product's slug to match
the catalog's URL style; the whole-catalog views use their own ``social-proof/``
prefix so the catalog's ``products/<slug>`` pattern can never capture them.
"""

from django.urls import path

from apps.social_proof.views import (
    ProductViewersView,
    ProductViewRecordView,
    RecentSalesView,
    ViewerCountsBatchView,
)

urlpatterns = [
    path(
        "products/<slug:slug>/view/",
        ProductViewRecordView.as_view(),
        name="product-view",
    ),
    path(
        "products/<slug:slug>/viewers/",
        ProductViewersView.as_view(),
        name="product-viewers",
    ),
    path(
        "social-proof/viewers/",
        ViewerCountsBatchView.as_view(),
        name="viewer-counts-batch",
    ),
    path(
        "social-proof/recent-sales/",
        RecentSalesView.as_view(),
        name="recent-sales",
    ),
]
