"""API views for the accounts app.

Staff authentication (JWT login/refresh/logout, ``/me/`` profile, password
change and reset) plus the shared staff ``Address`` directory. There is no
public registration — accounts exist only for staff/admin access. Address
list/detail views are staff-wide (manager/support): the directory holds
repeat-delivery addresses reused across orders, so any staff member can read
or edit any entry.
"""

from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from apps.accounts.models import Address
from apps.accounts.permissions import IsManagerOrSupport
from apps.accounts.selectors import list_addresses
from apps.accounts.serializers import (
    AddressSerializer,
    ChangePasswordSerializer,
    ConfirmPasswordResetSerializer,
    LogoutSerializer,
    RequestPasswordResetSerializer,
    UserSerializer,
)
from apps.accounts.services import (
    change_password,
    reset_password,
    send_password_reset_email,
)


class LoginView(TokenObtainPairView):
    """Issue a JWT access/refresh pair for a staff account.

    Public (``AllowAny``) by design — login precedes authentication.
    Rate-limited with the dedicated ``auth_login`` scope to blunt brute-force
    guessing of passwords. Only staff-role accounts are served here: with no
    customer storefront login, a customer-role credential has no reachable
    endpoint and is rejected outright.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request, *args, **kwargs):
        """Authenticate a staff account and return the token pair.

        Args:
            request: the POST request with credentials.

        Returns:
            Response: the token pair, ``403`` for a valid customer-role
                credential, or ``401`` for bad credentials.
        """
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0]) from exc

        user = serializer.user
        if user is not None and not (
            user.is_superuser
            or user.has_role("manager", "support", "analyst", "courier")
        ):
            return Response(
                {"detail": "This login is for staff accounts only."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


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


class CurrentUserView(generics.RetrieveUpdateAPIView):
    """Return or update the authenticated caller's own profile.

    GET returns the caller's profile; PATCH updates the writable profile
    fields (``username``, ``phone_number``, ``first_name``, ``last_name``) on
    the caller themselves. Requires authentication; the object is always the
    caller, so no URL parameter and no ownership check is needed.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_read"
    serializer_class = UserSerializer

    def get_object(self):
        """Return the authenticated user for the request."""
        return self.request.user


class AddressPagination(PageNumberPagination):
    """Page the address list with a client-selectable, bounded page size.

    In addition to ``page`` (provided by the project default), accepts a
    ``page_size`` capped at 100 so a single client cannot request an unbounded
    page.
    """

    page_size_query_param = "page_size"
    max_page_size = 100


class AddressListCreateView(generics.ListCreateAPIView):
    """List the shared directory or add a repeat-delivery address to it.

    Staff-only (manager/support): the directory is reused across orders, so
    any staff member reads every entry. The list is paginated with a
    client-selectable bounded page size and supports exact filtering on
    ``is_default`` and ``county``, free-text search across the contact/location
    fields, and ordering by the declared fields.
    """

    permission_classes = [IsManagerOrSupport]
    serializer_class = AddressSerializer
    pagination_class = AddressPagination
    filterset_fields = ["is_default", "county"]
    search_fields = [
        "label",
        "recipient_name",
        "phone_number",
        "county",
        "area_name",
        "landmark_description",
        "building_or_estate",
    ]
    ordering_fields = ["created_at", "county", "area_name", "label"]

    @property
    def throttle_scope(self):
        """Rate writes with ``auth_write`` and reads with ``auth_read``."""
        if self.request and self.request.method == "GET":
            return "auth_read"
        return "auth_write"

    def get_queryset(self):
        """Return the whole shared directory, newest first."""
        return list_addresses()

    def perform_create(self, serializer):
        """Save the entry unlinked — directory rows belong to no account."""
        serializer.save(user=None)


class AddressRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a single directory entry (staff only)."""

    permission_classes = [IsManagerOrSupport]
    serializer_class = AddressSerializer
    queryset = Address.objects.all()

    @property
    def throttle_scope(self):
        """Rate writes with ``auth_write`` and reads with ``auth_read``."""
        if self.request and self.request.method == "GET":
            return "auth_read"
        return "auth_write"
