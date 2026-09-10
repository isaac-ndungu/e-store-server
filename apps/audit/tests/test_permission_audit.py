"""Endpoint security-posture audit.

Walks every mounted view and enforces the platform's declared invariants
rather than trusting each app's spot checks:

- every view declares an explicit ``permission_classes`` (nothing silently
  relies on the framework default), and
- every view pins a named ``throttle_scope`` that exists in the configured
  rate table, and in particular every anonymously-reachable view is rate
  limited.

A regression in one app's permission classes that never got re-tested against
an earlier app's endpoints surfaces here as a single failure naming the view.
"""

import inspect

from django.test import TestCase
from django.urls import get_resolver
from rest_framework import permissions
from rest_framework.settings import api_settings
from rest_framework.views import APIView

# A permission check that grants access without authentication.
_ANONYMOUS_PERMISSIONS = {
    permissions.AllowAny,
    permissions.IsAuthenticatedOrReadOnly,
}


def _iter_api_views():
    """Yield every mounted view class under the root URL configuration."""
    resolver = get_resolver()

    def walk(patterns):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                yield from walk(pattern.url_patterns)
            elif hasattr(pattern.callback, "cls"):
                view_class = pattern.callback.cls
                if inspect.isclass(view_class) and issubclass(view_class, APIView):
                    if view_class is APIView:
                        continue
                    yield view_class

    yield from walk(resolver.url_patterns)


def _iter_app_views():
    """Yield every project-owned view class mounted under the root URL config.

    Views from third-party packages (the DRF router's ``APIRootView``, the
    Spectacular schema view) are deliberately excluded: they come pre-built
    and inherit the site defaults rather than making an app-level decision.
    """
    for view_class in _iter_api_views():
        if view_class.__module__.startswith("apps."):
            yield view_class


def _declared_attr(view_class, attr):
    """Return the first declaration of ``attr`` before a DRF base, or None.

    Walks the MRO and keeps the first class attribute, stopping at the first
    class owned by the DRF package so an attribute set on a shared app-level
    base (for example the analytics views' reporting base) still counts as
    explicit, while the framework default does not.

    Args:
        view_class (type): the view class.
        attr (str): the attribute name.

    Returns:
        object | None: the declared value, or None when no app-level class
            declares it.
    """
    for klass in view_class.__mro__:
        if klass.__module__.startswith("rest_framework"):
            break
        if attr in klass.__dict__:
            return klass.__dict__[attr]
    return None


class EndpointSecurityPostureTests(TestCase):
    """Enforce explicit permission and throttle declarations per endpoint."""

    def test_every_view_declares_an_explicit_permission_class(self):
        """No view may silently rely on the framework's default permission."""
        defaults = set(api_settings.DEFAULT_PERMISSION_CLASSES)
        offending = []
        for view_class in sorted(_iter_app_views(), key=lambda cls: cls.__name__):
            declared = _declared_attr(view_class, "permission_classes")
            if declared is None:
                offending.append(
                    f"{view_class.__module__}.{view_class.__name__} has no explicit "
                    "permission_classes"
                )
            elif declared == defaults:
                offending.append(
                    f"{view_class.__module__}.{view_class.__name__} maps to the "
                    "default permission set without an explicit decision"
                )
        self.assertEqual(offending, [])

    def test_every_view_pins_a_configured_throttle_scope(self):
        """Every view names a throttle scope that exists in the rate table."""
        known_scopes = set(api_settings.DEFAULT_THROTTLE_RATES)
        offending = []
        for view_class in sorted(_iter_app_views(), key=lambda cls: cls.__name__):
            scope = _declared_attr(view_class, "throttle_scope")
            if scope is None:
                offending.append(
                    f"{view_class.__module__}.{view_class.__name__} has no "
                    "throttle_scope (unthrottled)"
                )
                continue
            if not isinstance(scope, str):
                continue
            if scope not in known_scopes:
                offending.append(
                    f"{view_class.__module__}.{view_class.__name__} uses unknown "
                    f"throttle scope {scope!r}"
                )
        self.assertEqual(offending, [])

    def test_anonymous_reachable_views_are_rate_limited(self):
        """A view reachable without authentication must carry a throttle scope."""
        offending = []
        for view_class in sorted(_iter_app_views(), key=lambda cls: cls.__name__):
            permission_classes = _declared_attr(view_class, "permission_classes")
            if permission_classes is None:
                continue
            if not set(permission_classes) & _ANONYMOUS_PERMISSIONS:
                continue
            scope = _declared_attr(view_class, "throttle_scope")
            if scope is None:
                offending.append(
                    f"{view_class.__module__}.{view_class.__name__} is reachable "
                    "anonymously without a throttle scope"
                )
        self.assertEqual(offending, [])


class RoleRestrictionAuditTests(TestCase):
    """Cross-check revenue- and fulfilment-role restrictions across the API.

    The full matrix lives in the launch-readiness document. These assertions
    pin the pairs most likely to regress: analytics and dashboard data is
    manager-and-analyst-only, and order/return fulfilment actions are gated by
    named roles rather than Django's generic staff flag.
    """

    def test_analytics_views_are_manager_or_analyst_only(self):
        """Revenue reports require the manager or analyst role."""
        self._assert_shares_permission("apps.analytics.views", "IsManagerOrAnalyst")

    def test_dashboard_views_are_manager_or_analyst_only(self):
        """Dashboard widgets require the manager or analyst role."""
        self._assert_shares_permission("apps.dashboard.views", "IsManagerOrAnalyst")

    def test_fulfilment_actions_use_named_roles_not_is_staff(self):
        """Order/return status actions gate on manager/support, not is_staff."""
        expected_views = {
            "apps.orders.views.OrderStatusUpdateView",
            "apps.returns.views.ReturnRequestStaffListView",
            "apps.returns.views.ReturnRequestStaffDetailView",
            "apps.returns.views.ReturnApproveView",
            "apps.returns.views.ReturnRejectView",
            "apps.returns.views.ReturnCloseView",
            "apps.returns.views.ReturnReceiveItemView",
            "apps.returns.views.ReturnRefundView",
            "apps.returns.views.OrderPreShipmentCancelView",
        }
        offending = []
        for view_class in sorted(_iter_app_views(), key=lambda cls: cls.__name__):
            identifier = f"{view_class.__module__}.{view_class.__name__}"
            if identifier not in expected_views:
                continue
            names = {
                perm.__name__
                for perm in (_declared_attr(view_class, "permission_classes") or [])
            }
            if "IsManagerOrSupport" not in names:
                offending.append(
                    f"{identifier} allows {sorted(names) or ['<none declared>']}"
                )
            if "IsStaff" in names:
                offending.append(
                    f"{identifier} uses the generic IsStaff flag instead of a role"
                )
        self.assertEqual(offending, [])

    def _assert_shares_permission(self, module_prefix, permission_name):
        """Assert every view under a module uses the named role permission."""
        offending = []
        for view_class in sorted(_iter_api_views(), key=lambda cls: cls.__name__):
            if not view_class.__module__.startswith(module_prefix):
                continue
            names = {
                perm.__name__
                for perm in (_declared_attr(view_class, "permission_classes") or [])
            }
            if permission_name not in names:
                offending.append(
                    f"{view_class.__module__}.{view_class.__name__} allows "
                    f"{sorted(names) or ['<none declared>']}"
                )
        self.assertEqual(offending, [])
