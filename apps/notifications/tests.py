"""Tests for the notifications app.

Covers the SMS service abstraction (success/failure logging through the
provider mock, the failure contract, provider-exception propagation, OTP
redaction from the stored message, and pre-dispatch phone validation), the
staff-only test-send endpoint (permissions, throttle, validation, audit
logging), notification-log retrieval with filters, and masking of internal
error details from non-staff callers.
"""

from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.notifications.models import NotificationLog
from apps.notifications.services import (
    _send_africastalking,
    send_email,
    send_notification,
    send_otp_sms,
    send_sms,
)

SEND_TEST_URL = reverse("api:notifications:notification-send-test-sms")
LOG_LIST_URL = reverse("api:notifications:notification-log-list")
FAILED_LOGS_URL = reverse("api:notifications:notification-log-failed")
DLR_URL = reverse("api:notifications:notification-delivery-report")

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


def _make_superuser(password="BossPass123!"):
    """Create and return a superuser (full admin access)."""
    return User.objects.create_superuser(
        email="boss@example.com",
        username="boss",
        password=password,
        phone_number="+254700000000",
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
    def test_send_otp_sms_redacts_otp_from_stored_message(self, mock_provider):
        """The provider gets the full text, but the log never stores the OTP."""
        log = send_otp_sms("+254712345678", "482913")
        self.assertEqual(log.purpose, "otp")
        self.assertEqual(log.status, "sent")

        # The provider receives the full rendered body including the code.
        provider_message = mock_provider.call_args[0][1]
        self.assertIn("482913", provider_message)
        self.assertIn("Do not share this code", provider_message)

        # The audit log stores a masked body, so the live OTP is never in the DB.
        self.assertNotIn("482913", log.message)
        self.assertIn("******", log.message)
        self.assertIn("Do not share this code", log.message)

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

    @mock.patch("apps.notifications.services._send_via_provider")
    def test_garbage_number_never_reaches_provider(self, mock_provider):
        """A malformed phone is rejected before any provider call is made.

        Validating pre-dispatch avoids wasting an SMS credit on a call
        that would fail anyway, and leaves no audit log row behind.
        """
        from rest_framework import serializers

        with self.assertRaises(serializers.ValidationError):
            send_sms("not-a-phone", "Hi", "test")
        mock_provider.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)

    @mock.patch("apps.notifications.services._send_via_provider")
    def test_local_phone_is_normalized_before_provider_call(self, mock_provider):
        """A local-format number is normalized to E.164 before dispatch."""
        mock_provider.return_value = {
            "success": True,
            "message_id": "",
            "response": {},
            "error": "",
        }
        send_sms("0712345678", "Hi", "test")
        # The provider receives the normalized E.164 form.
        sent_to = mock_provider.call_args[0][0]
        self.assertEqual(sent_to, "+254712345678")

    def test_failure_contract_returns_failed_log_not_exception(self):
        """A provider-reported failure returns a ``failed`` log, not an error.

        The checkout flow depends on this contract: a failed or timed-out
        send is reported via ``log.status == 'failed'`` so the caller can
        tell the customer the code could not be sent, rather than raising
        an exception and losing the audit trail.
        """

        def _fail(recipient, message, sender_id=""):
            return {
                "success": False,
                "message_id": "",
                "response": {},
                "error": "Request timed out",
            }

        with mock.patch(
            "apps.notifications.services._send_via_provider", side_effect=_fail
        ):
            log = send_sms("+254712345678", "Hi", "test")

        self.assertEqual(log.status, "failed")
        self.assertEqual(log.error_message, "Request timed out")
        self.assertEqual(NotificationLog.objects.get().status, "failed")


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
        """A provider failure is returned with ``failed`` status.

        The non-superuser sees a masked error (raw provider text is not
        leaked to staff without admin access).
        """
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(SEND_TEST_URL, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(
            response.data["error_message"],
            "Send failed. See Django admin for details.",
        )
        self.assertEqual(NotificationLog.objects.get().error_message, "Network timeout")

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
        """The admin cannot create, edit, or delete log entries in place."""
        from django.contrib.admin.sites import AdminSite

        from apps.notifications.admin import NotificationLogAdmin

        admin_instance = NotificationLogAdmin(NotificationLog, AdminSite())
        self.assertFalse(admin_instance.has_add_permission(request=None))
        self.assertFalse(
            admin_instance.has_change_permission(request=None, obj=NotificationLog())
        )
        self.assertFalse(
            admin_instance.has_delete_permission(request=None, obj=NotificationLog())
        )


class SecurityHardeningTests(APITestCase):
    """Exercises the security hardening applied to the app."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        self.superuser = _make_superuser()
        self.customer = _make_customer()

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": False,
            "message_id": "",
            "response": {},
            "error": "Provider rejected recipient",
        },
    )
    def test_superuser_sees_raw_error(self, mock_provider):
        """A superuser sees the raw provider error in the log output."""
        _login(self.client, "boss@example.com", "BossPass123!")
        response = self.client.post(SEND_TEST_URL, self.payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        log_id = response.data["id"]
        detail_url = reverse("api:notifications:notification-log-detail", args=[log_id])
        response = self.client.get(detail_url)
        self.assertEqual(response.data["error_message"], "Provider rejected recipient")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": False,
            "message_id": "",
            "response": {},
            "error": "Provider rejected recipient",
        },
    )
    def test_non_superuser_staff_sees_masked_error(self, mock_provider):
        """A staff (non-admin) user sees a masked error, not raw provider text."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.post(SEND_TEST_URL, self.payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        log_id = response.data["id"]
        detail_url = reverse("api:notifications:notification-log-detail", args=[log_id])
        response = self.client.get(detail_url)
        self.assertEqual(
            response.data["error_message"],
            "Send failed. See Django admin for details.",
        )

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "id_1",
            "response": {},
            "error": "",
        },
    )
    def test_idempotency_key_prevents_duplicate_send(self, mock_provider):
        """A repeated POST with the same idempotency key sends once."""
        _login(self.client, "boss@example.com", "BossPass123!")
        first = self.client.post(
            SEND_TEST_URL,
            self.payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY="key-123",
        )
        second = self.client.post(
            SEND_TEST_URL,
            self.payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY="key-123",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(NotificationLog.objects.count(), 1)
        mock_provider.assert_called_once()

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "id_1",
            "response": {},
            "error": "",
        },
    )
    def test_idempotency_key_on_service(self, mock_provider):
        """send_sms dedupes on idempotency_key at the service level."""
        first = send_sms("+254712345678", "Hi", "test", idempotency_key="svc-key-1")
        second = send_sms("+254712345678", "Hi", "test", idempotency_key="svc-key-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(NotificationLog.objects.count(), 1)
        mock_provider.assert_called_once()

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={"success": True, "message_id": "", "response": {}, "error": ""},
    )
    def test_per_recipient_rate_limit_blocks_extra_sends(self, mock_provider):
        """The per-recipient outbound rate limit rejects a 6th send."""
        from rest_framework import serializers

        for _ in range(5):
            send_sms("+254712345678", "Hi", "test")
        with self.assertRaisesMessage(
            serializers.ValidationError, "Too many SMS sends to +254712345678"
        ):
            send_sms("+254712345678", "Hi", "test")
        self.assertEqual(mock_provider.call_count, 5)

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={"success": True, "message_id": "", "response": {}, "error": ""},
    )
    def test_message_is_sanitized_via_endpoint(self, mock_provider):
        """HTML tags are stripped from the message through the send endpoint."""
        _login(self.client, "boss@example.com", "BossPass123!")
        response = self.client.post(
            SEND_TEST_URL,
            {
                "recipient": "+254712345678",
                "message": "<script>alert('x')</script>Hello <b>world</b>",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("<script>", response.data["message"])
        self.assertNotIn("<b>", response.data["message"])
        self.assertIn("Hello world", response.data["message"])

    def payload(self):
        """Return a valid test-SMS payload."""
        return {"recipient": "+254712345678", "message": "Test SMS"}


class EmailServiceTests(APITestCase):
    """Exercises the email-sending service abstraction."""

    def setUp(self):
        cache.clear()

    @mock.patch("django.core.mail.send_mail", return_value=1)
    def test_send_email_creates_sent_log(self, mock_send_mail):
        """A successful email send records a ``sent`` email log."""
        log = send_email(
            "ops@example.com", "Subject", "Body text", purpose="transactional"
        )
        self.assertEqual(log.channel, "email")
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.recipient, "ops@example.com")
        self.assertEqual(NotificationLog.objects.filter(channel="email").count(), 1)
        mock_send_mail.assert_called_once()

    @mock.patch("django.core.mail.send_mail", side_effect=OSError("smtp down"))
    def test_send_email_records_failure(self, mock_send_mail):
        """A provider failure is recorded with ``failed`` status."""
        log = send_email(
            "ops@example.com", "Subject", "Body text", purpose="transactional"
        )
        self.assertEqual(log.status, "failed")
        self.assertIn("smtp down", log.error_message)


class NotificationInfrastructureTests(APITestCase):
    """Exercises templates, low-stock alerts, and delivery reports."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        NotificationLog.objects.all().delete()

    def test_template_registry_renders(self):
        """A registered template renders with the supplied context."""
        from apps.notifications.notification_templates import render_message

        message = render_message("sms", "otp", code="482913", expiry_minutes=10)
        self.assertIn("482913", message)
        self.assertIn("10 minutes", message)

    def test_template_registry_rejects_unknown_key(self):
        """An unregistered template key raises ValueError."""
        from apps.notifications.notification_templates import render_message

        with self.assertRaises(ValueError):
            render_message("sms", "nonexistent_purpose", code="123")

    def test_send_notification_forwards_to_sms(self):
        """send_notification routes to send_sms for the sms channel."""
        with mock.patch(
            "apps.notifications.services._send_via_provider",
            return_value={
                "success": True,
                "message_id": "id_1",
                "response": {"SMSMessageData": {"NumSegments": 1}},
                "error": "",
            },
        ):
            log = send_notification(
                "sms",
                "test",
                "+254712345678",
                template_key="test",
                context={"body": "Hello"},
            )
        self.assertEqual(log.channel, "sms")
        self.assertEqual(log.status, "sent")

    @mock.patch("django.core.mail.send_mail", return_value=1)
    def test_send_notification_forwards_to_email(self, mock_send_mail):
        """send_notification routes to send_email for the email channel."""
        log = send_notification(
            "email",
            "transactional",
            "ops@example.com",
            context={"subject": "Hi", "body": "Body"},
            subject="Hi",
            body="Body",
        )
        self.assertEqual(log.channel, "email")
        self.assertEqual(log.status, "sent")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_9",
            "response": {"SMSMessageData": {"NumSegments": 2}},
            "error": "",
        },
    )
    def test_sms_stores_segments(self, mock_provider):
        """The provider-reported segment count is stored on the log."""
        log = send_sms("+254712345678", "x" * 200, "transactional")
        self.assertEqual(log.segments, 2)

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_dlr",
            "response": {},
            "error": "",
        },
    )
    def test_delivery_report_marks_delivered(self, mock_provider):
        """A delivery report updates the log to ``delivered``."""
        log = send_sms("+254712345678", "Hi", "transactional")
        response = self.client.post(
            DLR_URL,
            {"id": log.provider_message_id, "status": "Success"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        log.refresh_from_db()
        self.assertEqual(log.status, "delivered")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_fail",
            "response": {},
            "error": "",
        },
    )
    def test_delivery_report_marks_failed(self, mock_provider):
        """A failing delivery report updates the log to ``failed``."""
        log = send_sms("+254712345678", "Hi", "transactional")
        response = self.client.post(
            DLR_URL,
            {"id": log.provider_message_id, "status": "Failed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        log.refresh_from_db()
        self.assertEqual(log.status, "failed")

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "ATXid_term",
            "response": {},
            "error": "",
        },
    )
    def test_delivery_report_ignores_unknown_id(self, mock_provider):
        """A report for an unknown provider id is a no-op."""
        response = self.client.post(
            DLR_URL, {"id": "nope", "status": "Success"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class LowStockAlertTests(APITestCase):
    """Exercises the low-stock staff alert."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()

    @mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "",
            "response": {"SMSMessageData": {"NumSegments": 1}},
            "error": "",
        },
    )
    def test_low_stock_notifies_all_staff_with_phones(self, mock_provider):
        """Every staff user with a phone number is alerted."""
        from apps.notifications.services import notify_low_stock

        User.objects.create_user(
            email="ops@example.com",
            username="ops",
            password="OpsPass123!",
            phone_number="+254700111222",
            is_staff=True,
        )
        User.objects.create_user(
            email="nol@example.com",
            username="nol",
            password="NoPass123!",
            phone_number="",
            is_staff=True,
        )

        class _Variant:
            name = "Kettle"
            sku = "KTL-1"

        class _Warehouse:
            name = "Nairobi Main"

        logs = notify_low_stock(_Variant(), _Warehouse(), quantity=3, threshold=5)

        recipients = {log.recipient for log in logs}
        self.assertIn("+254712345678", recipients)
        self.assertIn("+254700111222", recipients)
        self.assertNotIn("", recipients)
        self.assertEqual(len(logs), 2)


class NotificationLogDetailTests(APITestCase):
    """Exercises the single-log detail endpoint."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        _make_customer()
        self.log = NotificationLog.objects.create(
            channel="sms",
            purpose="test",
            recipient="+254712345678",
            message="Hi",
            status="sent",
            sent_by=self.staff,
        )
        self.detail_url = reverse(
            "api:notifications:notification-log-detail", args=[self.log.id]
        )

    def test_anonymous_cannot_get_log_detail(self):
        """An unauthenticated caller is rejected (401)."""
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_cannot_get_log_detail(self):
        """A plain customer token is rejected (403)."""
        _login(self.client, "buyer@example.com", "CustomerPass123!")
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_get_log_detail(self):
        """A staff user can retrieve a single log."""
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], self.log.id)
        self.assertEqual(response.data["status"], "sent")


class ComposableFilterTests(APITestCase):
    """Verifies the composable log-filter queryset."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()

    def _seed(self):
        NotificationLog.objects.create(
            channel="sms",
            purpose="otp",
            recipient="+254712345678",
            message="OTP",
            status="sent",
            sent_by=self.staff,
        )
        NotificationLog.objects.create(
            channel="sms",
            purpose="order_update",
            recipient="+254700000000",
            message="Order",
            status="failed",
            error_message="rejected",
            sent_by=self.staff,
        )

    def test_combined_filters_compose(self):
        """Combining recipient and status narrows correctly (no priority bug)."""

        self._seed()
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(
            LOG_LIST_URL, {"recipient": "+254712345678", "status": "sent"}
        )
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["purpose"], "otp")

    def test_purpose_and_recipient_compose(self):
        """Purpose and recipient are both applied (not silently dropped)."""

        self._seed()
        _login(self.client, "manager@example.com", "StaffPass123!")
        response = self.client.get(
            LOG_LIST_URL,
            {
                "purpose": "otp",
                "recipient": "+254700000000",
            },
        )
        self.assertEqual(response.data["count"], 0)

    def test_selector_unit(self):
        """The composable selector applies all filters together."""
        self._seed()
        from apps.notifications.selectors import get_notification_logs

        qs = get_notification_logs(recipient="+254712345678", purpose="otp")
        self.assertEqual(list(qs.values_list("purpose", flat=True)), ["otp"])
        self.assertEqual(qs.count(), 1)
