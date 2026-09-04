"""DRF permission classes mapping staff roles to endpoint access.

Business endpoints are gated by the user's ``role`` rather than the coarse
``is_staff`` boolean, so a courier token can reach courier actions (e.g.
recording a COD collection) while a manager sees fulfilment operations and an
analyst sees revenue — and none of the three spill into the others. Django's
admin stays gated by ``is_staff``; these classes gate the API.

Each class grants access when the caller is authenticated and their role (or
superuser status, via ``User.has_role``) is among the roles the class allows.
"""

from rest_framework import permissions


class HasRolePermission(permissions.BasePermission):
    """Base permission granting access to callers holding a role.

    Subclasses set ``allowed_roles`` to the role keys they admit. The class
    attribute cannot be overrideable per-instance, so it is read off ``type``.
    """

    allowed_roles = ()

    def has_permission(self, request, view):
        """Return whether the caller is authenticated and holds an allowed role.

        Args:
            request: the incoming request.
            view: the DRF view.

        Returns:
            bool: True when the caller is authenticated and holds an allowed
                role (or is a superuser).
        """
        user = request.user
        return bool(
            user and user.is_authenticated and user.has_role(*type(self).allowed_roles)
        )


class IsManager(HasRolePermission):
    """Allow only users with the manager role."""

    allowed_roles = ("manager",)


class IsManagerOrSupport(HasRolePermission):
    """Allow users with the manager or support role."""

    allowed_roles = ("manager", "support")


class IsManagerOrAnalyst(HasRolePermission):
    """Allow users with the manager or analyst role."""

    allowed_roles = ("manager", "analyst")


class IsCourier(HasRolePermission):
    """Allow only users with the courier role."""

    allowed_roles = ("courier",)


class IsAnyStaffRole(HasRolePermission):
    """Allow any non-customer staff role (manager, support, analyst, courier)."""

    allowed_roles = ("manager", "support", "analyst", "courier")
