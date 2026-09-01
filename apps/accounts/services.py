"""Business logic for the accounts app.

Views stay thin; user creation (with email normalization and password
validation) lives here so every caller uses the same entry point.
"""

import re

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from apps.accounts.models import User


def normalize_email(email):
    """Return an email lowercased and stripped of surrounding whitespace.

    Uniqueness and login lookups both run against the normalized value so
    ``Foo@X.com`` and ``foo@x.com`` cannot register as separate accounts.

    Args:
        email (str): the raw address submitted by the client.

    Returns:
        str: the normalized address.
    """
    return email.strip().lower()


def normalize_phone_number(value):
    """Normalize a Kenyan phone number to E.164 (``+254...``) form.

    Strips punctuation and whitespace, converts a leading ``0`` to ``+254``,
    and sets a country code on a bare national number so all stored phones
    share one format. An already-prefixed international number is left as-is
    once it looks like a 12-digit ``254`` number.

    Args:
        value (str): the raw phone number.

    Returns:
        str: the normalized number, or the trimmed input if unrecognizable.
    """
    if not value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    if len(digits) == 12 and digits.startswith("254") and not value.startswith("+"):
        return f"+{digits}"
    if value.startswith("+") and digits.startswith("254") and len(digits) == 12:
        return f"+{digits}"
    return value.strip()


def validate_phone_number(value):
    """Return a normalized phone or raise a validation error.

    Validates the number against an E.164-style pattern (``+`` plus 8-15
    digits) after normalization, so a clearly malformed or partial number is
    rejected before it is stored. All write paths call this so the stored phone
    is always normalized and the same shape.

    Args:
        value (str): the raw phone number from the client.

    Returns:
        str: the normalized, validated E.164 number.

    Raises:
        serializers.ValidationError: if the number cannot be normalized into a
            valid E.164 form.
    """
    from rest_framework import serializers

    normalized = normalize_phone_number(value)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
        raise serializers.ValidationError(
            "Enter a valid phone number in international format, e.g. +254712345678."
        )
    return normalized


def register_user(email, username, password, phone_number):
    """Create and return a new user after validating the inputs.

    Normalizes the email and phone number and runs Django's password validators
    so a weak password is rejected rather than stored, and every write path
    lands on the same normalized values. Does not create an ``Address`` or
    ``LoyaltyAccount`` — those are handled elsewhere in the application.

    Args:
        email (str): login credential; normalized before storage.
        username (str): display/handle, stored and checked as-is.
        password (str): raw password to validate and hash.
        phone_number (str): platform contact number, normalized before storage.

    Returns:
        User: the newly created user.

    Raises:
        ValidationError: if the password fails Django's password validators.
    """
    normalized_email = normalize_email(email)
    normalized_phone = normalize_phone_number(phone_number)
    validate_password(password)
    return User.objects.create_user(
        email=normalized_email,
        username=username,
        password=password,
        phone_number=normalized_phone,
    )


def send_password_reset_email(email, request):
    """Send a password-reset link to ``email`` if an account exists.

    Uses Django's ``PasswordResetTokenGenerator`` so no separate reset-token
    table is needed. No result is returned to the caller — an unknown address
    silently does nothing so the endpoint cannot be used to enumerate accounts.

    Args:
        email (str): the address to send reset instructions to.
        request (HttpRequest): used to build an absolute reset link.

    Returns:
        int: the number of messages sent (0 when no account matches).
    """
    user = User.objects.filter(email__iexact=normalize_email(email)).first()
    if user is None:
        return 0

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    domain = get_current_site(request).domain
    reset_url = f"{settings.RESET_LINK_BASE}{uid}/{token}/"

    subject = render_to_string(
        "accounts/password_reset_subject.txt", {"user": user}
    ).strip()
    message = render_to_string(
        "accounts/password_reset_email.html",
        {"user": user, "reset_url": reset_url, "domain": domain},
    )
    return send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email])


def decode_uid(uid):
    """Decode a base64-encoded user primary key, or return None.

    Args:
        uid (str): the URL-safe base64 user id from a reset link.

    Returns:
        int | None: the user primary key, or None if it cannot be decoded.
    """
    try:
        return urlsafe_base64_decode(force_str(uid)).decode()
    except TypeError, ValueError, OverflowError, UnicodeDecodeError:
        return None


def reset_password(uid, token, new_password):
    """Set a new password if the ``uid``/``token`` pair is valid.

    Verifies the reset token against the user and validates the new password
    through Django's validators before saving. Invalid or expired tokens, or
    unknown uids, raise ``ValidationError`` so the caller can reject the flow.

    Args:
        uid (str): URL-safe base64 user id from the reset link.
        token (str): one-time reset token.
        new_password (str): the new password to set.

    Returns:
        User: the user whose password was changed.

    Raises:
        serializers.ValidationError: if the reset data is invalid or expired.
    """
    from rest_framework import serializers

    decoded = decode_uid(uid)
    if decoded is None:
        raise serializers.ValidationError("This reset link is invalid.")
    user = User.objects.filter(pk=decoded).first()
    if user is None or not default_token_generator.check_token(user, token):
        raise serializers.ValidationError("This reset link is invalid or has expired.")

    try:
        validate_password(new_password, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"new_password": exc.messages}) from None

    user.set_password(new_password)
    user.save(update_fields=["password"])
    return user


def revoke_all_refresh_tokens(user):
    """Blacklist every outstanding refresh token for ``user``.

    Used after a password change or account deactivation so that tokens issued
    to other sessions can no longer be refreshed. Safe to call repeatedly; each
    token is blacklisted idempotently and already-blacklisted tokens are
    skipped.

    Args:
        user (User): the account whose outstanding refresh tokens to revoke.

    Returns:
        int: the number of tokens newly blacklisted.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    outstanding = OutstandingToken.objects.filter(
        user=user, expires_at__gt=timezone.now()
    ).exclude(blacklistedtoken__isnull=False)
    blacklisted = []
    for token in outstanding:
        blacklisted.append(BlacklistedToken.objects.create(token=token))
    return len(blacklisted)


def change_password(user, current_password, new_password):
    """Validate the caller's current password and set a new one.

    Requires the supplied ``current_password`` to match the account before the
    new password is set, and runs the new password through Django's password
    validators. After the change, all outstanding refresh tokens for the user
    are revoked so other sessions are signed out while the current one keeps
    its short-lived access token.

    Args:
        user (User): the account whose password is changing.
        current_password (str): the caller's existing password, must match.
        new_password (str): the new password to set.

    Returns:
        User: the user whose password was changed.

    Raises:
        serializers.ValidationError: if the current password is wrong or the
            new one fails Django's password validators.
    """
    from rest_framework import serializers

    if not user.check_password(current_password):
        raise serializers.ValidationError(
            {"current_password": "Your current password is incorrect."}
        )

    try:
        validate_password(new_password, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"new_password": exc.messages}) from None

    user.set_password(new_password)
    user.save(update_fields=["password"])
    revoke_all_refresh_tokens(user)
    return user


def deactivate_account(user, password):
    """Soft-deactivate an account by clearing ``is_active``.

    The account row (and all orders, addresses, and other history that
    reference it) is preserved; only login is disabled. Confirms the supplied
    ``password`` matches so an attacker who has stolen a session cannot
    silently deactivate the victim's account. All outstanding refresh tokens
    are revoked so existing sessions cannot refresh.

    Args:
        user (User): the account to deactivate.
        password (str): the account password, must match to proceed.

    Returns:
        User: the deactivated user.

    Raises:
        serializers.ValidationError: if the password is incorrect.
    """
    from rest_framework import serializers

    if not user.check_password(password):
        raise serializers.ValidationError({"password": "Your password is incorrect."})

    user.is_active = False
    user.save(update_fields=["is_active"])
    revoke_all_refresh_tokens(user)
    return user
