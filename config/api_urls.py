"""Top-level API URL configuration.

The DRF DefaultRouter is declared here and individual app routers — catalog,
cart, orders, payments, etc. — register their viewsets into it as they are
built. As a result the /api/v1/ mount point exists from day one and grows app
by app.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

router = DefaultRouter()

urlpatterns = [
    path("v1/", include(router.urls)),
    path("v1/", include(("apps.core.urls", "core"), namespace="core")),
    path("v1/", include(("apps.accounts.urls", "accounts"), namespace="accounts")),
    path(
        "v1/",
        include(
            ("apps.notifications.urls", "notifications"), namespace="notifications"
        ),
    ),
    path("v1/", include(("apps.catalog.urls", "catalog"), namespace="catalog")),
    path("v1/", include(("apps.inventory.urls", "inventory"), namespace="inventory")),
    path("v1/", include(("apps.shipping.urls", "shipping"), namespace="shipping")),
    path(
        "v1/",
        include(("apps.collections.urls", "collections"), namespace="collections"),
    ),
]
