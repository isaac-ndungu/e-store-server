"""API views for the accounts app.

Implements registration, JWT login/refresh/logout, the ``/me/`` profile
retrieve/update endpoint, password change and account deactivation, and
per-user ``Address`` CRUD. Registration is deliberately public
(``AllowAny``); everything else requires an authenticated user. Address
retrieve/update/delete enforce ownership via ``get_object()`` to prevent IDOR.
"""

from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from apps.accounts.models import Address
from apps.accounts.selectors import get_addresses_for_user
from apps.accounts.serializers import (
    AddressSerializer,
    ChangePasswordSerializer,
    ConfirmPasswordResetSerializer,
    DeactivateAccountSerializer,
    LogoutSerializer,
    RegisterSerializer,
    RequestPasswordResetSerializer,
    UserSerializer,
)
from apps.accounts.services import (
    change_password,
    deactivate_account,
    reset_password,
    send_password_reset_email,
)


class RegisterView(generics.CreateAPIView):
    """Create a new account from email, username, password, and phone number.

    Public (``AllowAny``) by design — registration happens before any
    authentication exists. Rate-limited with the dedicated ``auth_write`` scope
    to blunt automated signup spam.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_write"
    serializer_class = RegisterSerializer


class LoginView(TokenObtainPairView):
    """Issue a JWT access/refresh pair from email + password credentials.

    Public (``AllowAny``) by design — login precedes authentication.
    Rate-limited with the dedicated ``auth_login`` scope to blunt brute-force
    guessing of passwords.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"


class RefreshView(TokenRefreshView):
    """Return a fresh access token from a valid refresh token.

    With refresh rotation enabled this also mints a new refresh token and
    blacklists the old one. Public (``AllowAny``) by design — the refresh
    check itself authenticates the request. Rate-limited with the
    ``auth_reauth`` scope to limit token-refresh churn.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_reauth"


class LogoutView(APIView):
    """Blacklist a presented refresh token so it can no longer be used.

    Uses tiny JWT's ``RefreshToken`` directly, with no refresh middleware.
    ``blacklist()`` is idempotent (it uses ``get_or_create``), and a token that
    is already blacklisted (from an earlier logout or rotation) is treated as a
    successful no-op so a replayed logout cannot fail. Rate limited with the
    ``auth_write`` scope.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_write"
    serializer_class = LogoutSerializer

    def post(self, request):
        """Blacklist the supplied refresh token and return 204.

        Args:
            request: the POST request carrying the ``refresh`` token.

        Returns:
            Response: ``204 No Content`` once the token is blacklisted (or is
            already blacklisted), or ``400`` for a genuinely invalid token.

        Raises:
            ValidationError: if the token cannot be parsed or verified.
        """
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError as exc:
            if "blacklisted" in str(exc).lower():
                return Response(status=status.HTTP_204_NO_CONTENT)
            raise ValidationError({"detail": str(exc)}) from exc

        return Response(status=status.HTTP_204_NO_CONTENT)


class RequestPasswordResetView(APIView):
    """Send a password-reset link to the supplied email.

    Public (``AllowAny``) by design — the requested action precedes login. The
    response is always the same success message whether or not the email exists,
    so the endpoint cannot be used to enumerate registered accounts. Rate
    limited with the ``auth_reauth`` scope.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_reauth"
    serializer_class = RequestPasswordResetSerializer

    def post(self, request):
        """Send the reset email (if an account exists) and return 202.

        Args:
            request: the POST request carrying the ``email``.

        Returns:
            Response: ``202 Accepted`` with a generic message either way.
        """
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        send_password_reset_email(serializer.validated_data["email"], request)
        return Response(
            {
                "detail": "If an account exists with that email, reset instructions "
                "have been sent."
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ConfirmPasswordResetView(APIView):
    """Set a new password using a uid and reset token from the link.

    Public (``AllowAny``) by design — the reset token itself authenticates the
    request. Rate limited with the ``auth_write`` scope.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_write"
    serializer_class = ConfirmPasswordResetSerializer

    def post(self, request):
        """Validate the reset data and set the new password.

        Args:
            request: the POST request carrying ``uid``, ``token``, and
                ``new_password``.

        Returns:
            Response: ``204 No Content`` on success, or ``400`` on invalid data.

        Raises:
            ValidationError: if the reset link is invalid/expired or the new
                password fails Django's validators.
        """
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        reset_password(
            serializer.validated_data["uid"],
            serializer.validated_data["token"],
            serializer.validated_data["new_password"],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChangePasswordView(APIView):
    """Set a new password for the authenticated caller.

    Requires authentication and the caller's current password. All other
    sessions are revoked on success (via the service) so a changed credential
    cannot be raked with a stolen refresh token. Rate limited with the
    ``auth_write`` scope.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_write"
    serializer_class = ChangePasswordSerializer

    def post(self, request):
        """Validate and apply the requested password change.

        Args:
            request: the POST request carrying ``current_password`` and
                ``new_password``.

        Returns:
            Response: ``204 No Content`` on success, or ``400`` if the current
                password is wrong or the new one is invalid.
        """
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        change_password(
            request.user,
            serializer.validated_data["current_password"],
            serializer.validated_data["new_password"],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeactivateAccountView(APIView):
    """Soft-deactivate the authenticated caller's account.

    Requires authentication and the account password as an explicit
    confirmation. ``is_active`` is cleared so the account can no longer log in,
    while all order history and related records are preserved. All outstanding
    refresh tokens are revoked. Rate limited with the ``auth_write`` scope.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_write"
    serializer_class = DeactivateAccountSerializer

    def post(self, request):
        """Deactivate the account after confirming the password.

        Args:
            request: the POST request carrying the account ``password``.

        Returns:
            Response: ``204 No Content`` on success, or ``400`` if the password
                is incorrect.
        """
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        deactivate_account(request.user, serializer.validated_data["password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserView(generics.RetrieveUpdateAPIView):
    """Return or update the authenticated caller's own profile.

    GET returns the caller's profile; PATCH updates the writable profile
    fields (``username``, ``phone_number``, ``first_name``, ``last_name``) on
    the caller themselves. Requires authentication; the object is always the
    caller, so no URL parameter and no ownership check is needed.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        """Return the authenticated user for the request."""
        return self.request.user


class AddressListCreateView(generics.ListCreateAPIView):
    """List a user's addresses or add a new one to their account.

    Requires authentication and is scoped to the caller's own addresses — a
    user can never see another account's addresses here.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AddressSerializer

    def get_queryset(self):
        """Return only the authenticated caller's addresses."""
        return get_addresses_for_user(self.request.user)

    def perform_create(self, serializer):
        """Attach the new address to the authenticated caller."""
        serializer.save(user=self.request.user)


class AddressRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a single address owned by the caller.

    Requires authentication. Ownership is enforced in ``get_object()`` so a
    caller cannot reach another account's address via its primary key (IDOR).
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AddressSerializer

    def get_object(self):
        """Return the address only if it belongs to the authenticated caller.

        Returns:
            Address: the requested address owned by ``request.user``.

        Raises:
            Http404: if no address with the URL pk exists for ``request.user``.
        """
        return get_object_or_404(Address, pk=self.kwargs["pk"], user=self.request.user)
