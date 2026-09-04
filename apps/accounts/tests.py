"""Tests for the accounts app.

Covers registration (including email normalization and password validation),
JWT login/refresh/rotation/logout, password reset (request + confirm),
password change and account deactivation, the ``/me/`` retrieve/update
endpoint, per-user Address CRUD with ownership enforcement (cross-user and
missing resources both return 404), and the ``auth_login`` / ``auth_write``
throttle scopes.
"""

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Address, User

REGISTER_URL = reverse("api:accounts:register")
LOGIN_URL = reverse("api:accounts:login")
REFRESH_URL = reverse("api:accounts:refresh")
LOGOUT_URL = reverse("api:accounts:logout")
PASSWORD_RESET_URL = reverse("api:accounts:password-reset-request")
PASSWORD_RESET_CONFIRM_URL = reverse("api:accounts:password-reset-confirm")
PASSWORD_CHANGE_URL = reverse("api:accounts:password-change")
ACCOUNT_DEACTIVATE_URL = reverse("api:accounts:account-deactivate")
ME_URL = reverse("api:accounts:me")
ADDRESS_LIST_URL = reverse("api:accounts:address-list-create")

# Throttle scopes configured in settings.py; keep in sync.
AUTH_LOGIN_RATE_LIMIT = 3
AUTH_WRITE_RATE_LIMIT = 10


class RegisterTests(APITestCase):
    """Exercises the public account-registration endpoint."""

    def setUp(self):
        cache.clear()
        self.payload = {
            "email": "buyer@example.com",
            "username": "buyer",
            "password": "StrongPass123!",
            "phone_number": "+254712345678",
        }

    def test_register_is_public_and_creates_user(self):
        """An anonymous caller can register and a user is created."""
        response = self.client.post(REGISTER_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get()
        self.assertEqual(user.email, "buyer@example.com")
        self.assertEqual(user.username, "buyer")
        self.assertEqual(user.phone_number, "+254712345678")
        self.assertFalse(user.phone_verified)

    def test_register_rejects_duplicate_email(self):
        """Registering the same email twice yields a 400, not a second account."""
        self.client.post(REGISTER_URL, self.payload, format="json")
        response = self.client.post(REGISTER_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 1)

    def test_register_rejects_duplicate_username(self):
        """Registering the same username with a different email is rejected."""
        self.client.post(REGISTER_URL, self.payload, format="json")
        response = self.client.post(
            REGISTER_URL,
            {
                "email": "other@example.com",
                "username": "buyer",
                "password": "StrongPass123!",
                "phone_number": "+254700000000",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 1)

    def test_register_rejects_weak_password(self):
        """A one-character password fails Django's password validators."""
        response = self.client.post(
            REGISTER_URL, {**self.payload, "password": "a"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 0)

    def test_register_normalizes_email(self):
        """A mixed-case address is stored lowercase so login always matches."""
        response = self.client.post(
            REGISTER_URL,
            {**self.payload, "email": "  Buyer@Example.COM  "},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.get().email, "buyer@example.com")

    def test_register_email_lookup_is_case_insensitive(self):
        """A second registration differing only by case is rejected as a dup."""
        self.client.post(REGISTER_URL, self.payload, format="json")
        response = self.client.post(
            REGISTER_URL,
            {**self.payload, "email": "BUYER@EXAMPLE.COM"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 1)

    def test_register_normalizes_phone_number(self):
        """A local-format phone number is normalized to E.164 on registration."""
        response = self.client.post(
            REGISTER_URL, {**self.payload, "phone_number": "0712345678"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.get().phone_number, "+254712345678")

    def test_register_rejects_malformed_phone_number(self):
        """A clearly invalid phone number is rejected with a 400."""
        response = self.client.post(
            REGISTER_URL, {**self.payload, "phone_number": "not-a-phone"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 0)

    def test_register_throttles_after_rate_limit(self):
        """Bursting past the auth_write rate limit yields HTTP 429."""
        for i in range(AUTH_WRITE_RATE_LIMIT):
            response = self.client.post(
                REGISTER_URL,
                {
                    "email": f"u{i}@example.com",
                    "username": f"u{i}",
                    "password": "StrongPass123!",
                    "phone_number": f"+25470000000{i}",
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(
            REGISTER_URL,
            {
                "email": "throttled@example.com",
                "username": "throttled",
                "password": "StrongPass123!",
                "phone_number": "+254700000001",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class LoginTests(APITestCase):
    """Exercises JWT login, refresh, and the me endpoint."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        self.login_payload = {
            "email": "buyer@example.com",
            "password": "StrongPass123!",
        }

    def test_login_returns_token_pair(self):
        """A valid email + password yields access and refresh tokens."""
        response = self.client.post(LOGIN_URL, self.login_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_rejects_wrong_password(self):
        """An incorrect password yields 401, not a token pair."""
        response = self.client.post(
            LOGIN_URL,
            {**self.login_payload, "password": "WrongPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_rejects_unknown_email(self):
        """An unknown email yields 401 (no user enumeration signal)."""
        response = self.client.post(
            LOGIN_URL,
            {"email": "nobody@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_returns_new_access_token(self):
        """A valid refresh token mints a fresh access token."""
        login = self.client.post(LOGIN_URL, self.login_payload, format="json")
        response = self.client.post(
            REFRESH_URL, {"refresh": login.data["refresh"]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_login_throttles_after_rate_limit(self):
        """Bursting past the auth_login rate limit yields HTTP 429."""
        payload = {"email": "nobody@example.com", "password": "WrongPass123!"}
        for _ in range(AUTH_LOGIN_RATE_LIMIT):
            response = self.client.post(LOGIN_URL, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.post(LOGIN_URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_me_requires_authentication(self):
        """An unauthenticated call to /me/ is rejected."""
        response = self.client.get(ME_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_current_user(self):
        """A JWT-authenticated call returns the caller's own profile."""
        login = self.client.post(LOGIN_URL, self.login_payload, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        response = self.client.get(ME_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "buyer@example.com")
        self.assertEqual(response.data["username"], "buyer")
        self.assertEqual(response.data["phone_number"], "+254712345678")


class MeUpdateTests(APITestCase):
    """Exercises PATCH on the /me/ endpoint for profile updates."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")

    def test_patch_requires_authentication(self):
        """An unauthenticated PATCH to /me/ is rejected."""
        self.client.credentials()
        response = self.client.patch(ME_URL, {"first_name": "Jane"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_patch_updates_profile_fields(self):
        """A logged-in caller can update writable profile fields."""
        response = self.client.patch(
            ME_URL,
            {"first_name": "Jane", "last_name": "Buyer", "username": "jane_buyer"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["first_name"], "Jane")
        self.assertEqual(response.data["last_name"], "Buyer")
        self.assertEqual(response.data["username"], "jane_buyer")

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Jane")
        self.assertEqual(self.user.last_name, "Buyer")
        self.assertEqual(self.user.username, "jane_buyer")

    def test_patch_normalizes_and_validates_phone_number(self):
        """A local-format phone number is normalized to E.164 on update."""
        response = self.client.patch(
            ME_URL, {"phone_number": "0712345678"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["phone_number"], "+254712345678")
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "+254712345678")

    def test_patch_rejects_malformed_phone_number(self):
        """A clearly invalid phone number is rejected with a 400."""
        response = self.client.patch(
            ME_URL, {"phone_number": "not-a-phone"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "+254712345678")

    def test_patch_rejects_username_taken_by_another_user(self):
        """Setting a username another account already uses yields a 400."""
        User.objects.create_user(
            email="other@example.com",
            username="other_user",
            password="StrongPass123!",
            phone_number="+254700000000",
        )
        response = self.client.patch(ME_URL, {"username": "other_user"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "buyer")

    def test_patch_cannot_change_email(self):
        """The login email is read-only and cannot be altered via /me/."""
        response = self.client.patch(
            ME_URL, {"email": "hacked@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "buyer@example.com")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "buyer@example.com")

    def test_patch_cannot_set_privileged_fields(self):
        """Fields like is_staff are ignored because they are not writable."""
        response = self.client.patch(
            ME_URL, {"is_staff": True, "is_superuser": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)


class RefreshRotationAndLogoutTests(APITestCase):
    """Exercises refresh-token rotation and blacklisting on logout."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        self.login_payload = {
            "email": "buyer@example.com",
            "password": "StrongPass123!",
        }

    def test_refresh_rotates_and_returns_new_refresh(self):
        """A refresh call mints a new refresh token alongside the access token."""
        login = self.client.post(LOGIN_URL, self.login_payload, format="json")
        response = self.client.post(
            REFRESH_URL, {"refresh": login.data["refresh"]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertNotEqual(response.data["refresh"], login.data["refresh"])

    def test_rotated_refresh_token_cannot_be_reused(self):
        """The old refresh token is blacklisted once a new one is issued."""
        login = self.client.post(LOGIN_URL, self.login_payload, format="json")
        self.client.post(REFRESH_URL, {"refresh": login.data["refresh"]}, format="json")
        reuse = self.client.post(
            REFRESH_URL, {"refresh": login.data["refresh"]}, format="json"
        )
        self.assertEqual(reuse.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_blacklists_refresh_token(self):
        """After logout the same refresh token can no longer mint access tokens."""
        login = self.client.post(LOGIN_URL, self.login_payload, format="json")
        logout = self.client.post(
            LOGOUT_URL, {"refresh": login.data["refresh"]}, format="json"
        )
        self.assertEqual(logout.status_code, status.HTTP_204_NO_CONTENT)

        refresh = self.client.post(
            REFRESH_URL, {"refresh": login.data["refresh"]}, format="json"
        )
        self.assertEqual(refresh.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_is_idempotent(self):
        """A replayed logout of an already-blacklisted token is a 204 no-op."""
        login = self.client.post(LOGIN_URL, self.login_payload, format="json")
        self.client.post(LOGOUT_URL, {"refresh": login.data["refresh"]}, format="json")
        replay = self.client.post(
            LOGOUT_URL, {"refresh": login.data["refresh"]}, format="json"
        )
        self.assertEqual(replay.status_code, status.HTTP_204_NO_CONTENT)

    def test_logout_rejects_invalid_token(self):
        """Logout with a non-refresh token is rejected with a 400."""
        response = self.client.post(
            LOGOUT_URL, {"refresh": "not-a-token"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AddressTests(APITestCase):
    """Exercises per-user Address CRUD with ownership enforcement."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        self.other = User.objects.create_user(
            email="other@example.com",
            username="other",
            password="StrongPass123!",
            phone_number="+254700000000",
        )
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.address_payload = {
            "label": "Home",
            "recipient_name": "Jane Buyer",
            "phone_number": "+254712345678",
            "county": "Nairobi",
            "area_name": "Westlands",
            "landmark_description": "Near Sarit Centre",
            "building_or_estate": "ABC Apartments",
        }

    def test_anonymous_cannot_access_addresses(self):
        """An unauthenticated caller cannot list or create addresses."""
        self.client.credentials()
        response = self.client.get(ADDRESS_LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        create = self.client.post(ADDRESS_LIST_URL, self.address_payload, format="json")
        self.assertEqual(create.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_cannot_mutate_an_existing_address(self):
        """A guest cannot retrieve, update, or delete a stored address by id."""
        created = self.client.post(
            ADDRESS_LIST_URL, self.address_payload, format="json"
        )
        detail_url = reverse("api:accounts:address-detail", args=[created.data["id"]])

        self.client.credentials()
        self.assertEqual(
            self.client.get(detail_url).status_code, status.HTTP_401_UNAUTHORIZED
        )
        self.assertEqual(
            self.client.patch(
                detail_url, {"label": "Stolen"}, format="json"
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.delete(detail_url).status_code, status.HTTP_401_UNAUTHORIZED
        )

        self.assertEqual(Address.objects.get(id=created.data["id"]).label, "Home")
        self.assertEqual(Address.objects.count(), 1)

    def test_create_and_list_address(self):
        """A logged-in user can create then list their own addresses."""
        create = self.client.post(ADDRESS_LIST_URL, self.address_payload, format="json")
        self.assertEqual(create.status_code, status.HTTP_201_CREATED)
        address_id = create.data["id"]

        detail_url = reverse("api:accounts:address-detail", args=[address_id])
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["recipient_name"], "Jane Buyer")

        listing = self.client.get(ADDRESS_LIST_URL)
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data["count"], 1)

    def test_update_and_delete_address(self):
        """A logged-in user can update then delete their own address."""
        created = self.client.post(
            ADDRESS_LIST_URL, self.address_payload, format="json"
        )
        address_id = created.data["id"]
        detail_url = reverse("api:accounts:address-detail", args=[address_id])

        update = self.client.patch(detail_url, {"label": "Work"}, format="json")
        self.assertEqual(update.status_code, status.HTTP_200_OK)
        self.assertEqual(update.data["label"], "Work")

        delete = self.client.delete(detail_url)
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Address.objects.count(), 0)

    def test_create_address_normalizes_phone_number(self):
        """A local-format delivery phone is normalized to E.164 on create."""
        response = self.client.post(
            ADDRESS_LIST_URL,
            {**self.address_payload, "phone_number": "0712345678"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["phone_number"], "+254712345678")
        self.assertEqual(Address.objects.get().phone_number, "+254712345678")

    def test_update_address_normalizes_phone_number(self):
        """A local-format delivery phone is normalized to E.164 on update."""
        created = self.client.post(
            ADDRESS_LIST_URL, self.address_payload, format="json"
        )
        address_id = created.data["id"]
        detail_url = reverse("api:accounts:address-detail", args=[address_id])

        update = self.client.patch(
            detail_url, {"phone_number": "0700000000"}, format="json"
        )
        self.assertEqual(update.status_code, status.HTTP_200_OK)
        self.assertEqual(update.data["phone_number"], "+254700000000")

    def test_create_address_rejects_malformed_phone_number(self):
        """A clearly invalid delivery phone is rejected with a 400."""
        response = self.client.post(
            ADDRESS_LIST_URL,
            {**self.address_payload, "phone_number": "not-a-phone"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Address.objects.count(), 0)

    def test_cannot_access_another_users_address(self):
        """User B cannot retrieve or modify user A's address (IDOR guard)."""
        created = self.client.post(
            ADDRESS_LIST_URL, self.address_payload, format="json"
        )
        address_id = created.data["id"]
        detail_url = reverse("api:accounts:address-detail", args=[address_id])

        other_login = self.client.post(
            LOGIN_URL,
            {"email": "other@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {other_login.data['access']}"
        )

        retrieve = self.client.get(detail_url)
        self.assertEqual(retrieve.status_code, status.HTTP_404_NOT_FOUND)

        update = self.client.patch(detail_url, {"label": "Stolen"}, format="json")
        self.assertEqual(update.status_code, status.HTTP_404_NOT_FOUND)

        self.assertEqual(Address.objects.get(id=address_id).label, "Home")

    def test_missing_address_is_indistinguishable_from_another_users(self):
        """A nonexistent id and another user's id both return 404, not 403.

        This locks the convention: cross-user access must not be reported
        differently from a plain missing record, or the id scheme would leak
        which ids other accounts use.
        """
        self.client.post(ADDRESS_LIST_URL, self.address_payload, format="json")

        missing_url = reverse("api:accounts:address-detail", args=[99999])
        missing = self.client.get(missing_url)
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

        other_login = self.client.post(
            LOGIN_URL,
            {"email": "other@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {other_login.data['access']}"
        )
        cross_user = self.client.get(
            reverse(
                "api:accounts:address-detail",
                args=[Address.objects.get().id],
            )
        )
        self.assertEqual(cross_user.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_cannot_list_my_addresses(self):
        """User B's address list never contains user A's addresses."""
        self.client.post(ADDRESS_LIST_URL, self.address_payload, format="json")

        other_login = self.client.post(
            LOGIN_URL,
            {"email": "other@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {other_login.data['access']}"
        )
        listing = self.client.get(ADDRESS_LIST_URL)
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data["count"], 0)

    def test_list_supports_filter_search_and_default_pagination(self):
        """The address list filters, searches, and paginates by default."""
        nairobi = {**self.address_payload, "county": "Nairobi", "label": "Home"}
        mombasa = {**self.address_payload, "county": "Mombasa", "label": "Work"}
        self.client.post(
            ADDRESS_LIST_URL, {**nairobi, "is_default": True}, format="json"
        )
        self.client.post(ADDRESS_LIST_URL, mombasa, format="json")

        county = self.client.get(ADDRESS_LIST_URL, {"county": "Mombasa"})
        self.assertEqual(county.status_code, status.HTTP_200_OK)
        self.assertEqual(county.data["count"], 1)
        self.assertEqual(county.data["results"][0]["county"], "Mombasa")

        default = self.client.get(ADDRESS_LIST_URL, {"is_default": "true"})
        self.assertEqual(default.status_code, status.HTTP_200_OK)
        self.assertEqual(default.data["count"], 1)
        self.assertTrue(default.data["results"][0]["is_default"])

        search = self.client.get(ADDRESS_LIST_URL, {"search": "Sarit"})
        self.assertEqual(search.status_code, status.HTTP_200_OK)
        self.assertEqual(search.data["count"], 2)

        page = self.client.get(ADDRESS_LIST_URL, {"page_size": 1})
        self.assertEqual(page.status_code, status.HTTP_200_OK)
        self.assertEqual(page.data["count"], 2)
        self.assertEqual(len(page.data["results"]), 1)
        self.assertIsNotNone(page.data["next"])

    def test_list_supports_ordering(self):
        """The address list can be ordered by county, area_name, and label."""
        self.client.post(
            ADDRESS_LIST_URL,
            {
                **self.address_payload,
                "label": "Home",
                "county": "Mombasa",
                "area_name": "Nyali",
            },
            format="json",
        )
        self.client.post(
            ADDRESS_LIST_URL,
            {
                **self.address_payload,
                "label": "Work",
                "county": "Nairobi",
                "area_name": "Westlands",
            },
            format="json",
        )
        by_county = self.client.get(ADDRESS_LIST_URL, {"ordering": "county"})
        self.assertEqual(by_county.status_code, status.HTTP_200_OK)
        counties = [a["county"] for a in by_county.data["results"]]
        self.assertEqual(counties, ["Mombasa", "Nairobi"])

        by_county_desc = self.client.get(ADDRESS_LIST_URL, {"ordering": "-county"})
        self.assertEqual(by_county_desc.status_code, status.HTTP_200_OK)
        counties_desc = [a["county"] for a in by_county_desc.data["results"]]
        self.assertEqual(counties_desc, ["Nairobi", "Mombasa"])

        by_area = self.client.get(ADDRESS_LIST_URL, {"ordering": "area_name"})
        self.assertEqual(by_area.status_code, status.HTTP_200_OK)
        areas = [a["area_name"] for a in by_area.data["results"]]
        self.assertEqual(areas, ["Nyali", "Westlands"])


class AddressDefaultTests(APITestCase):
    """Exercises the single-default invariant for a user's addresses."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.address_payload = {
            "label": "Work",
            "recipient_name": "Jane Buyer",
            "phone_number": "+254712345678",
            "county": "Nairobi",
            "area_name": "Westlands",
            "landmark_description": "Near Sarit Centre",
            "building_or_estate": "ABC Apartments",
        }

    def _create_default(self, label="Home"):
        """Create a default address and return its id."""
        response = self.client.post(
            ADDRESS_LIST_URL,
            {**self.address_payload, "label": label, "is_default": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data["id"]

    def test_creating_a_second_default_clears_the_first(self):
        """Only one default remains after two addresses are marked default."""
        first = self._create_default("Home")
        second = self._create_default("Work")

        others = Address.objects.filter(is_default=True)
        self.assertEqual(others.count(), 1)
        self.assertEqual(others.get().id, second)
        self.assertFalse(Address.objects.get(id=first).is_default)

    def test_switching_default_via_update_clears_the_old(self):
        """Marking a non-default address default clears the previous default."""
        first = self._create_default("Home")
        second_response = self.client.post(
            ADDRESS_LIST_URL,
            {**self.address_payload, "label": "Work", "is_default": False},
            format="json",
        )
        second = second_response.data["id"]
        self.assertFalse(Address.objects.get(id=second).is_default)
        self.assertTrue(Address.objects.get(id=first).is_default)

        response = self.client.patch(
            reverse("api:accounts:address-detail", args=[second]),
            {"is_default": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Address.objects.get(id=second).is_default)
        self.assertFalse(Address.objects.get(id=first).is_default)

    def test_clearing_default_does_not_touch_others(self):
        """Setting an address default to False affects only that address."""
        home = self._create_default("Home")
        work_response = self.client.post(
            ADDRESS_LIST_URL,
            {**self.address_payload, "label": "Work", "is_default": False},
            format="json",
        )
        work = work_response.data["id"]

        response = self.client.patch(
            reverse("api:accounts:address-detail", args=[home]),
            {"is_default": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Address.objects.filter(is_default=True).count(), 0)
        self.assertFalse(Address.objects.get(id=work).is_default)


class PasswordResetTests(APITestCase):
    """Exercises the requested-then-confirmed password reset flow."""

    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="OldPass123!",
            phone_number="+254712345678",
        )

    def _uid_token(self):
        """Return (uid, token) that validates for the test user."""
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        return uid, token

    def test_request_sends_reset_email(self):
        """A known email receives reset instructions with a valid link."""
        response = self.client.post(
            PASSWORD_RESET_URL, {"email": "buyer@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset", mail.outbox[0].body.lower())

    def test_request_does_not_reveal_unknown_email(self):
        """An unknown email still returns 202 and sends nothing."""
        response = self.client.post(
            PASSWORD_RESET_URL, {"email": "nobody@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(len(mail.outbox), 0)

    def test_confirm_sets_new_password(self):
        """A valid uid + token changes the password so the new one logs in."""
        uid, token = self._uid_token()
        response = self.client.post(
            PASSWORD_RESET_CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "NewPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPass123!"))

        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "NewPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)

    def test_confirm_rejects_old_password_after_reset(self):
        """The pre-reset password no longer works after a reset."""
        uid, token = self._uid_token()
        self.client.post(
            PASSWORD_RESET_CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "NewPass123!"},
            format="json",
        )
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "OldPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_confirm_rejects_invalid_uid(self):
        """A bogus uid is rejected with a 400 and the password is unchanged."""
        response = self.client.post(
            PASSWORD_RESET_CONFIRM_URL,
            {"uid": "not-a-uid", "token": "x", "new_password": "NewPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("OldPass123!"))

    def test_confirm_rejects_weak_password(self):
        """A weak new password fails Django's validators (400, no change)."""
        uid, token = self._uid_token()
        response = self.client.post(
            PASSWORD_RESET_CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "a"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("OldPass123!"))


class ChangePasswordTests(APITestCase):
    """Exercises the authenticated change-password endpoint."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")

    def test_change_requires_authentication(self):
        """An unauthenticated change-password call is rejected."""
        self.client.credentials()
        response = self.client.post(
            PASSWORD_CHANGE_URL,
            {"current_password": "StrongPass123!", "new_password": "NewPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_change_password_updates_credential(self):
        """A correct current password sets the new one and the old stops working."""
        response = self.client.post(
            PASSWORD_CHANGE_URL,
            {"current_password": "StrongPass123!", "new_password": "NewPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPass123!"))

        old_login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(old_login.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_change_rejects_wrong_current_password(self):
        """A wrong current password yields 400 and the old password still works."""
        response = self.client.post(
            PASSWORD_CHANGE_URL,
            {"current_password": "WrongPass123!", "new_password": "NewPass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("StrongPass123!"))

    def test_change_rejects_weak_new_password(self):
        """A weak new password fails Django's validators (400, no change)."""
        response = self.client.post(
            PASSWORD_CHANGE_URL,
            {"current_password": "StrongPass123!", "new_password": "a"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("StrongPass123!"))

    def test_change_revokes_outstanding_refresh_tokens(self):
        """Other refresh tokens stop working after a password change."""
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        refresh = login.data["refresh"]

        self.client.post(
            PASSWORD_CHANGE_URL,
            {"current_password": "StrongPass123!", "new_password": "NewPass123!"},
            format="json",
        )

        reused = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(reused.status_code, status.HTTP_401_UNAUTHORIZED)


class DeactivateAccountTests(APITestCase):
    """Exercises the soft account-deactivation endpoint."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="StrongPass123!",
            phone_number="+254712345678",
        )
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")

    def test_deactivate_requires_authentication(self):
        """An unauthenticated deactivate call is rejected."""
        self.client.credentials()
        response = self.client.post(
            ACCOUNT_DEACTIVATE_URL, {"password": "StrongPass123!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deactivate_clears_is_active(self):
        """A confirmed deactivation sets is_active False and blocks re-login."""
        response = self.client.post(
            ACCOUNT_DEACTIVATE_URL, {"password": "StrongPass123!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deactivate_rejects_wrong_password(self):
        """A wrong password leaves the account fully active."""
        response = self.client.post(
            ACCOUNT_DEACTIVATE_URL, {"password": "WrongPass123!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_deactivate_revokes_outstanding_refresh_tokens(self):
        """Existing refresh tokens stop working after deactivation."""
        login = self.client.post(
            LOGIN_URL,
            {"email": "buyer@example.com", "password": "StrongPass123!"},
            format="json",
        )
        refresh = login.data["refresh"]

        self.client.post(
            ACCOUNT_DEACTIVATE_URL, {"password": "StrongPass123!"}, format="json"
        )

        reused = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(reused.status_code, status.HTTP_401_UNAUTHORIZED)


class PiiMaskingTests(APITestCase):
    """Exercises PII masking helpers used before persisting audit data."""

    def test_mask_phone_keeps_prefix_and_tail(self):
        """mask_phone keeps a short prefix and the tail, masking the middle."""
        from apps.accounts.services import mask_phone

        self.assertEqual(mask_phone("+254712345678"), "+254*******78")
        self.assertEqual(mask_phone("+254700000001"), "+254*******01")

    def test_mask_phone_short_value(self):
        """A short or empty value is fully masked without crashing."""
        from apps.accounts.services import mask_phone

        self.assertEqual(mask_phone(""), "")
        self.assertEqual(mask_phone("12"), "**")

    def test_mask_email_keeps_domain_and_edges(self):
        """mask_email masks the local part but keeps the domain."""
        from apps.accounts.services import mask_email

        self.assertEqual(mask_email("ops@example.com"), "o*s@example.com")
        self.assertEqual(mask_email("a@b.co"), "*@b.co")

    def test_mask_email_no_at(self):
        """An email without an @ is returned unchanged."""
        from apps.accounts.services import mask_email

        self.assertEqual(mask_email("notanemail"), "notanemail")
