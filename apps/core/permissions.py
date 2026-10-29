"""Shared access-control rules for project endpoints."""

from django.conf import settings
from rest_framework import permissions


class IsStaffUser(permissions.BasePermission):
    """Allow only authenticated staff or superuser accounts."""

    message = "This action requires staff privileges."

    def has_permission(self, request, view):
        """Check if the user is authenticated and has staff status.

        Args:
            request: the incoming DRF request.
            view: the view being accessed.

        Returns:
            bool: True if the user is authenticated and is staff or superuser.
        """
        user = request.user
        return bool(
            user and user.is_authenticated and (user.is_staff or user.is_superuser)
        )


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
        user = request.user
        return bool(
            user and user.is_authenticated and (user.is_staff or user.is_superuser)
        )
