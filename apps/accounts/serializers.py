"""Serializers for the accounts app.

Defines the explicit readable/writable field lists for registration, the
``/me/`` user payload (with writable profile fields), password-change and
account-deactivation confirmation payloads, and the writable ``Address`` CRUD
serializer. Every write path normalizes and validates emails and phone
numbers so stored values keep one shape. Login itself is handled by
``simplejwt``'s ``TokenObtainPairSerializer`` (which reads ``USERNAME_FIELD`` =
email), so no login serializer is defined here.
"""

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.accounts.models import Address, User
from apps.accounts.services import (
    normalize_email,
    register_user,
    validate_phone_number,
)


class LogoutSerializer(serializers.Serializer):
    """Validate the refresh token presented for blacklisting on logout."""

    refresh = serializers.CharField()


class RequestPasswordResetSerializer(serializers.Serializer):
    """Validate the email address for a password-reset request."""

    email = serializers.EmailField()

    def validate_email(self, value):
        """Normalize the email before the reset email is created.

        Args:
            value (str): the raw submitted address.

        Returns:
            str: the normalized address.
        """
        return normalize_email(value)


class ConfirmPasswordResetSerializer(serializers.Serializer):
    """Validate the uid and token and the new password for a reset."""

    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )


class ChangePasswordSerializer(serializers.Serializer):
    """Validate current and new passwords for an authenticated password change.

    ``current_password`` must match the account's real password so a stolen
    session alone cannot change it; the new password runs through Django's
    validators in the service. Both fields are write-only.
    """

    current_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )
    new_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )


class DeactivateAccountSerializer(serializers.Serializer):
    """Validate the password that confirms an account deactivation.

    The password is required as an explicit confirmation so a token thief
    cannot deactivate a victim's account. Write-only.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})


class RegisterSerializer(serializers.ModelSerializer):
    """Create a new account from email, username, password, and phone number.

    Writable fields are explicitly listed — no client may set ``is_staff``,
    ``is_superuser``, or any other field. Email is normalized before the
    uniqueness check and the password passes through Django's password
    validators.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["email", "username", "password", "phone_number"]
        extra_kwargs = {
            "email": {"allow_blank": False},
            "username": {"allow_blank": False},
        }

    def validate_email(self, value):
        """Normalize the email and reject it if it is already registered.

        Args:
            value (str): the raw submitted address.

        Returns:
            str: the normalized address.

        Raises:
            serializers.ValidationError: if the normalized address is taken.
        """
        normalized = normalize_email(value)
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized

    def validate_phone_number(self, value):
        """Normalize and validate the registration contact phone number.

        Args:
            value (str): the raw submitted number.

        Returns:
            str: the normalized, validated E.164 number.
        """
        return validate_phone_number(value)

    def validate_password(self, value):
        """Run Django's password validators so weak passwords are rejected.

        Args:
            value (str): the raw submitted password.

        Returns:
            str: the validated password.

        Raises:
            serializers.ValidationError: if any password validator fails.
        """
        validate_password(value, self.instance)
        return value

    def create(self, validated_data):
        """Create the user via the service to reuse normalization/validation.

        Args:
            validated_data (dict): email, username, password, phone_number.

        Returns:
            User: the newly created account.
        """
        return register_user(**validated_data)


class UserSerializer(serializers.ModelSerializer):
    """Representation of the logged-in user for ``/me/``.

    ``username``, ``phone_number``, ``first_name``, and ``last_name`` are
    writable so the caller can update their own profile via PATCH. ``email``
    stays read-only — it is the login credential and changing it is handled
    separately. ``phone_number`` is normalized and validated so stored phones
    keep one shape. The writable field list is explicit; no client can set
    ``is_staff``, ``phone_verified``, or any other field.
    """

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "phone_number",
            "phone_verified",
            "first_name",
            "last_name",
            "date_joined",
        ]
        read_only_fields = ["id", "email", "phone_verified", "date_joined"]

    def validate_username(self, value):
        """Reject a username taken by another account.

        Args:
            value (str): the submitted handle.

        Returns:
            str: the trimmed, validated username.

        Raises:
            serializers.ValidationError: if another user already has the name.
        """
        queryset = User.objects.filter(username=value)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(
                "A user with this username already exists."
            )
        return value

    def validate_phone_number(self, value):
        """Normalize and validate the account contact phone number.

        Args:
            value (str): the raw submitted number.

        Returns:
            str: the normalized, validated E.164 number.
        """
        return validate_phone_number(value)


class AddressSerializer(serializers.ModelSerializer):
    """Slim detail representation of a user's delivery address.

    Readable all the way through; ownership is enforced in the view, not here.
    Setting ``is_default`` to true makes this the user's only default address —
    any other default for the same user is cleared, so a user never ends up
    with two defaults.
    """

    class Meta:
        model = Address
        fields = [
            "id",
            "label",
            "recipient_name",
            "phone_number",
            "county",
            "area_name",
            "landmark_description",
            "building_or_estate",
            "is_default",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        """Create the address and keep at most one default per user.

        Args:
            validated_data (dict): the validated address fields, including
                ``user`` injected by the view on save.

        Returns:
            Address: the newly created address.
        """
        if validated_data.get("is_default"):
            self._clear_other_defaults(validated_data["user"], keep_pk=None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        """Update the address, clearing other defaults if this becomes one.

        Args:
            instance (Address): the address being updated.
            validated_data (dict): the validated address fields.

        Returns:
            Address: the updated address.
        """
        if validated_data.get("is_default") and not instance.is_default:
            self._clear_other_defaults(instance.user, keep_pk=instance.pk)
        return super().update(instance, validated_data)

    def _clear_other_defaults(self, user, keep_pk):
        """Clear the default flag on every other address of ``user``.

        Args:
            user (User): the address owner whose other defaults to clear.
            keep_pk (int | None): the address primary key to leave untouched,
                or None when creating a brand-new address.
        """
        other_defaults = user.addresses.filter(is_default=True)
        if keep_pk is not None:
            other_defaults = other_defaults.exclude(pk=keep_pk)
        other_defaults.update(is_default=False)

    def validate_phone_number(self, value):
        """Normalize and validate the address delivery-contact phone number.

        Args:
            value (str): the raw submitted number.

        Returns:
            str: the normalized, validated E.164 number.
        """
        return validate_phone_number(value)
