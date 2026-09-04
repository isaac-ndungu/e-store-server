"""URL configuration for the project.

App-specific API endpoints are mounted at /api/v1/. The health/status endpoint
and the admin site are always available; static/media serving is wired for
development.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


def health_view(request):
    """Return a liveness/readiness probe for Postman, CI, and load balancers.

    Lets external tooling confirm the backend is up before any app-specific
    endpoint exists. Deliberately public and unauthenticated.

    Args:
        request: the incoming HTTP request (unused by the handler).

    Returns:
        JsonResponse: a fixed ``{"status": "ok"}`` payload.
    """
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_view, name="health"),
    path("api/", include(("config.api_urls", "api"), namespace="api")),
    path(
        "api/schema/",
        SpectacularAPIView.as_view(permission_classes=[]),
        name="schema",
    ),
    path(
        "api/swagger/",
        SpectacularSwaggerView.as_view(url_name="schema", permission_classes=[]),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema", permission_classes=[]),
        name="redoc",
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
