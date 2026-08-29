"""Top-level API URL configuration.

The DRF DefaultRouter is declared here (Step 0 scaffolding) and individual app
routers — catalog, cart, orders, payments, etc. — register their viewsets into
it as each Step of the roadmap (§12) is built. As a result the /api/v1/ mount
point exists from day one and grows app by app.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

router = DefaultRouter()

urlpatterns = [
    path('v1/', include(router.urls)),
]
