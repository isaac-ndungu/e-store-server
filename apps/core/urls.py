"""URL routes for the core app.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. These are read-only public
resource endpoints, so plain paths rather than a router.
"""

from django.urls import path

from apps.core.views import LegalInformationView, SiteConfigView

urlpatterns = [
    path("site-config/", SiteConfigView.as_view(), name="site-config"),
    path(
        "site-config/legal/",
        LegalInformationView.as_view(),
        name="site-config-legal",
    ),
]
