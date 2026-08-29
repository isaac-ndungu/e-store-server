"""URL configuration for config project.

App-specific API endpoints are mounted at /api/v1/ as each Step of the
implementation roadmap (§12) is built. Step 0 only provides the health/status
endpoint and admin; static/media serving is wired for development.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health_view(request):
    """Return a liveness/readiness probe for Postman, CI, and load balancers.

    Lets external tooling confirm the backend is up before any app-specific
    endpoint exists (Step 0). Deliberately public and unauthenticated.

    Returns:
        JsonResponse: a fixed ``{"status": "ok"}`` payload.
    """
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_view, name='health'),
    path('api/', include(('config.api_urls', 'api'), namespace='api')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
