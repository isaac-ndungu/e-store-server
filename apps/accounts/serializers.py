"""Serializers for the accounts app.

Defines the explicit readable/writable field lists for the ``/me/`` staff
payload (with writable profile fields), password-change confirmation, and the
writable ``Address`` directory serializer. Every write path normalizes and
validates emails and phone numbers so stored values keep one shape. Login
itself is handled by ``simplejwt``'s ``TokenObtainPairSerializer`` (which
reads ``USERNAME_FIELD`` = email), so no login serializer is defined here.
"""

from rest_framework import serializers

from apps.accounts.models import Address, User
from apps.accounts.services import normalize_email, validate_phone_number


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
        write_only=True,
        max_length=128,
        style={"input_type": "password"},
    )


class ChangePasswordSerializer(serializers.Serializer):
    """Validate current and new passwords for an authenticated password change.

    ``current_password`` must match the account's real password so a stolen
    session alone cannot change it; the new password runs through Django's
    validators in the service. Both fields are write-only.
    """

    current_password = serializers.CharField(
        write_only=True,
        max_length=128,
        style={"input_type": "password"},
    )
    new_password = serializers.CharField(
        write_only=True,
        max_length=128,
        style={"input_type": "password"},
    )


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
            "role",
            "first_name",
            "last_name",
            "date_joined",
        ]
        read_only_fields = ["id", "email", "phone_verified", "role", "date_joined"]

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
    """Detail representation of a shared delivery address.

    Staff manage the whole directory — ownership is not enforced here or in
    the view. Setting ``is_default`` to true clears the flag on every other
    entry so intake pre-fill has exactly one default.
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
        """Create the address, keeping a single directory-wide default.

        Args:
            validated_data (dict): the validated address fields.

        Returns:
            Address: the newly created address.
        """
        if validated_data.get("is_default"):
            self._clear_other_defaults(keep_pk=None)
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
            self._clear_other_defaults(keep_pk=instance.pk)
        return super().update(instance, validated_data)

    def _clear_other_defaults(self, keep_pk):
        """Clear the default flag on every other directory entry.

        Args:
            keep_pk (int | None): the address primary key to leave untouched,
                or None when creating a brand-new address.
        """
        other_defaults = Address.objects.filter(is_default=True)
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
