"""URL routes for the accounts app.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Staff authentication
endpoints live under ``auth/`` and the shared address directory under
``accounts/addresses/``.
"""

from django.urls import path

from apps.accounts.views import (
    AddressListCreateView,
    AddressRetrieveUpdateDestroyView,
    ChangePasswordView,
    ConfirmPasswordResetView,
    CurrentUserView,
    LoginView,
    LogoutView,
    RefreshView,
    RequestPasswordResetView,
)

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", RefreshView.as_view(), name="refresh"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/password-change/", ChangePasswordView.as_view(), name="password-change"),
    path(
        "auth/password-reset/",
        RequestPasswordResetView.as_view(),
        name="password-reset-request",
    ),
    path(
        "auth/password-reset/confirm/",
        ConfirmPasswordResetView.as_view(),
        name="password-reset-confirm",
    ),
    path("auth/me/", CurrentUserView.as_view(), name="me"),
    path(
        "accounts/addresses/",
        AddressListCreateView.as_view(),
        name="address-list-create",
    ),
    path(
        "accounts/addresses/<int:pk>/",
        AddressRetrieveUpdateDestroyView.as_view(),
        name="address-detail",
    ),
]
