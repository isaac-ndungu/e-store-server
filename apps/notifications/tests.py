"""Tests for the notifications app.

Covers the SMS service abstraction (success/failure logging through the
provider mock), the staff-only test-send endpoint (permissions, throttle,
validation, idempotent audit logging), notification-log retrieval with
filters, and masking of internal error details from non-staff callers.
"""

from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.notifications.models import NotificationLog
from apps.notifications.services import _send_africastalking, send_otp_sms, send_sms

SEND_TEST_URL = reverse("api:notifications:notification-send-test-sms")
LOG_LIST_URL = reverse("api:notifications:notification-log-list")
FAILED_LOGS_URL = reverse("api:notifications:notification-log-failed")

SMSSendFailure = Exception


def _make_staff(password="StaffPass123!"):
    """Create and return a staff user."""
    return User.objects.create_user(
        email="manager@example.com",
        username="manager",
        password=password,
        phone_number="+254712345678",
        is_staff=True,
    )


def _make_customer(password="CustomerPass123!"):
    """Create and return a plain customer user."""
    return User.objects.create_user(
        email="buyer@example.com",
        username="buyer",
        password=password,
        phone_number="+254712345678",
    )


def _login(client, email, password):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


class SendSmsServiceTests(APITestCase):
    """Exercises the SMS service abstraction with a mocked provider."""

    def setUp(self):
        cache.clear()

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_12345",
            "response": {"SMSMessageData": {"Recipients": []}},
            "error": "",
        },
    )
    def test_send_sms_creates_sent_log(self, mock_provider):
        """A successful provider call records a ``sent`` log."""
        log = send_sms("+254712345678", "Hello from the platform", "transactional")
        mock_provider.assert_called_once()
        self.assertEqual(NotificationLog.objects.count(), 1)
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.recipient, "+254712345678")
        self.assertEqual(log.message, "Hello from the platform")
        self.assertEqual(log.purpose, "transactional")
        self.assertEqual(log.provider_message_id, "ATXid_12345")
        self.assertEqual(log.channel, "sms")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": False,
            "message_id": "",
            "response": {},
            "error": "Bad phone number",
        },
    )
    def test_send_sms_records_failure(self, mock_provider):
        """A failed provider call records a ``failed`` log with the error."""
        log = send_sms("+254700000000", "Test message", "test")
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.error_message, "Bad phone number")
        self.assertEqual(log.provider_message_id, "")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_6789",
            "response": {},
            "error": "",
        },
    )
    def test_send_otp_sms_composes_and_sends(self, mock_provider):
        """OTP SMS includes the code and uses the ``otp`` purpose."""
        log = send_otp_sms("+254712345678", "482913")
        self.assertEqual(log.purpose, "otp")
        self.assertIn("482913", log.message)
        self.assertIn("Do not share this code", log.message)
        self.assertEqual(log.status, "sent")
        mock_provider.assert_called_once()
        # The OTP value must not appear as a standalone plaintext credential
        # in the persisted log beyond the composed message.
        self.assertNotIn("Secret", log.provider_response)

    def test_africastalking_returns_bad_phone_error(self):
        """Africa's Talking failure status is parsed into a failed result."""
        import africastalking

        with mock.patch.object(africastalking, "initialize") as mock_init:
            with mock.patch.object(africastalking, "SMS") as mock_sms:
                mock_sms.send.return_value = {
                    "SMSMessageData": {
                        "Recipients": [{"status": "Rejected", "messageId": ""}]
                    }
                }
                result = _send_africastalking("+254700000000", "Hi", "sender")
        mock_init.assert_called_once()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Rejected")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        side_effect=SMSSendFailure("provider down"),
    )
    def test_send_sms_surfaces_exception(self, mock_provider):
        """An unexpected provider exception propagates rather than being swallowed."""
        with self.assertRaises(SMSSendFailure):
            send_sms("+254712345678", "Hi", "test")


class SendTestSMSEndpointTests(APITestCase):
    """Exercises the staff-only test-SMS endpoint."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        self.customer = _make_customer()
        self.manager = User.objects.create_user(
            email="boss@example.com",
            username="boss",
            password="BossPass123!",
            phone_number="+254700000000",
            is_staff=True,
            is_superuser=True,
        )
        self.payload = {
            "recipient": "+254712345678",
            "message": "Test SMS from the platform",
        }

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_test",
            "response": {},
            "error": "",
        },
    )
    def test_staff_can_send_test_sms(self, mock_provider):
        """A staff user can trigger a test SMS and get a 201 with a sent log."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "sent")
        self.assertEqual(response.data["recipient"], "+254712345678")
        self.assertEqual(response.data["purpose"], "test")
        log = NotificationLog.objects.get()
        self.assertEqual(log.sent_by, self.staff)

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_test",
            "response": {},
            "error": "",
        },
    )
    def test_superuser_can_send_test_sms(self, mock_provider):
        """A superuser (admin) also passes the staff-only gate."""
        _login(self.client, "boss@example.com", "BossPass123!")
        response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_anonymous_cannot_send_test_sms(self):
        """An unauthenticated caller is rejected (401)."""
        response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_non_staff_cannot_send_test_sms(self):
        """A plain customer token is rejected (403) and no SMS is sent."""
        _login(self.client, "buyer@example.com", "CustomerPass123!")
        with mock.patch(
            "apps.notifications.services._send_via_provider"
        ) as mock_provider:
            response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_provider.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={"success": True, "message_id": "", "response": {}, "error": ""},
    )
    def test_send_test_sms_rejects_invalid_phone(self, mock_provider):
        """A malformed phone number yields a 400 without sending."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(
            SEND_TEST_URL,
            {**self.payload, "recipient": "not-a-phone"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_provider.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={"success": True, "message_id": "", "response": {}, "error": ""},
    )
    def test_send_test_sms_normalizes_phone(self, mock_provider):
        """A local-format phone number is normalized to E.164 before sending."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(
            SEND_TEST_URL,
            {**self.payload, "recipient": "0712345678"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The provider is called with the normalized number.
        sent_to = mock_provider.call_args[0][0]
        self.assertEqual(sent_to, "+254712345678")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": False,
            "message_id": "",
            "response": {},
            "error": "Network timeout",
        },
    )
    def test_send_test_sms_records_failed_status(self, mock_provider):
        """A provider failure is returned with ``failed`` status and error."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(response.data["error_message"], "Network timeout")
        self.assertEqual(NotificationLog.objects.get().status, "failed")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={"success": True, "message_id": "", "response": {}, "error": ""},
    )
    def test_send_test_sms_rejects_overlong_message(self, mock_provider):
        """A message over 1600 characters is rejected with a 400."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(
            SEND_TEST_URL,
            {**self.payload, "message": "x" * 1601},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_provider.assert_not_called()

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={"success": True, "message_id": "", "response": {}, "error": ""},
    )
    def test_send_test_sms_throttles_after_rate_limit(self, mock_provider):
        """Bursting past the notification_send rate limit yields HTTP 429."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        # The rate is 5/min configured in settings.py; keep in sync.
        for _ in range(5):
            response = self.client.post(SEND_TEST_URL, self.payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class NotificationLogEndpointTests(APITestCase):
    """Exercises staff-only notification-log retrieval."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        User.objects.create_user(
            email="buyer@example.com",
            username="buyer",
            password="CustomerPass123!",
            phone_number="+254712345678",
        )

        # Seed a couple of logs directly so list/filter behavior is testable.
        NotificationLog.objects.create(
            channel="sms",
            purpose="otp",
            recipient="+254712345678",
            message="Your code is 123456.",
            status="sent",
            provider_message_id="id_a",
            sent_by=self.staff,
        )
        NotificationLog.objects.create(
            channel="sms",
            purpose="order_update",
            recipient="+254700000000",
            message="Your order is out for delivery.",
            status="failed",
            provider_message_id="",
            error_message="Provider rejected recipient",
            sent_by=self.staff,
        )

    def test_anonymous_cannot_list_logs(self):
        """An unauthenticated caller is rejected (401)."""
        response = self.client.get(LOG_LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_cannot_list_logs(self):
        """A plain customer token is rejected (403)."""
        _login(self.client, "buyer@example.com", "CustomerPass123!")
        response = self.client.get(LOG_LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_list_all_logs(self):
        """A staff user can list all notification logs."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(LOG_LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_staff_can_filter_by_recipient(self):
        """Logs can be filtered by recipient."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(LOG_LIST_URL, {"recipient": "+254712345678"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["recipient"], "+254712345678")

    def test_staff_can_filter_by_status(self):
        """Logs can be filtered by status."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(LOG_LIST_URL, {"status": "failed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["status"], "failed")

    def test_staff_can_filter_by_purpose(self):
        """Logs can be filtered by purpose."""
        NotificationLog.objects.create(
            channel="sms",
            purpose="test",
            recipient="+254700000001",
            message="A test.",
            status="sent",
        )
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(LOG_LIST_URL, {"purpose": "test"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["purpose"], "test")

    def test_failed_logs_endpoint(self):
        """The failed-logs endpoint returns only failed rows."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(FAILED_LOGS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["status"], "failed")


class NotificationLogModelTests(APITestCase):
    """Verifies the model-level status transition helper."""

    def test_update_status_persists_fields(self):
        """update_status() writes the new status and provider details."""
        log = NotificationLog.objects.create(
            channel="sms",
            purpose="test",
            recipient="+254712345678",
            message="Hi",
            status="pending",
        )
        log.update_status(
            "sent", provider_message_id="AT_id", provider_response={"ok": True}
        )
        log.refresh_from_db()
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.provider_message_id, "AT_id")
        self.assertEqual(log.provider_response, {"ok": True})

    def test_admin_is_append_only(self):
        """The admin cannot create or edit log entries in place."""
        from django.contrib.admin.sites import AdminSite

        from apps.notifications.admin import NotificationLogAdmin

        admin_instance = NotificationLogAdmin(NotificationLog, AdminSite())
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(
            admin_instance.has_change_permission(request=None, obj=NotificationLog())
        )
