"""URL routing for the social proof endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Both endpoints are public
storefront paths; the record route is a POST and the reader a GET. They live
under a product's slug to match the catalog's URL style — the more specific
two-segment suffix is resolved before any catalog detail route could match.
"""

from django.urls import path

from apps.social_proof.views import ProductViewersView, ProductViewRecordView

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
]
