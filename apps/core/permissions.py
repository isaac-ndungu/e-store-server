"""Shared access-control rules for project endpoints."""

from django.conf import settings
from rest_framework import permissions


class ApiDocsAccess(permissions.BasePermission):
    """Allow API documentation anonymously in development, staff-only otherwise.

    The OpenAPI schema and the Swagger/ReDoc pages describe every route and
    payload shape in the system. They are useful for local development but
    must not be an anonymous map of the production API.
    """

    def has_permission(self, request, view):
        """Return whether the caller may view the API documentation.

        Args:
            request: the incoming DRF request.
            view: the view being accessed.

        Returns:
            bool: True in development, or for an authenticated staff user in
                production.
        """
        if settings.API_DOCS_PUBLIC:
            return True
        return bool(request.user and request.user.is_staff)
