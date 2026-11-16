"""Tests for storefront order-intake email delivery.

The Resend SDK is mocked throughout so no network call is made. The access
gating, audit rows, inquiry delivery state, view wiring, resend endpoint, and
management command are all exercised through the real service path.
"""

import importlib
from io import StringIO
from unittest import mock

from django.apps import apps as django_apps
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.core.models import SiteConfig
from apps.inquiries.email import resend_order_intake_email, send_order_intake_email
from apps.inquiries.models import Inquiry
from apps.inquiries.services import record_inquiry
from apps.inquiries.tasks import send_order_intake_email_task
from apps.notifications.models import NotificationLog

SNAPSHOT = [
    {"sku": "KTL-1-V", "name": "Kettle", "quantity": 1, "price": "5000.00"},
]
CREATE_URL = reverse("api:inquiries:inquiry-create")


def _make_staff():
    """Create and return a staff user.

    Returns:
        User: the created staff account.
    """
    return User.objects.create_user(
        email="staff@example.com",
        username="staff",
        password="StrongPass123!",
        phone_number="+254712345678",
        is_staff=True,
    )


def _login(client):
    """Authenticate the test client as the staff user.

    Args:
        client: the DRF test client.
    """
    response = client.post(
        reverse("api:accounts:login"),
        {"email": "staff@example.com", "password": "StrongPass123!"},
        format="json",
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _set_intake_email(value):
    """Set the staff-editable order intake mailbox.

    Args:
        value (str): the mailbox value, or an empty string.
    """
    config = SiteConfig.load()
    config.order_intake_email = value
    config.save()


def _make_email_inquiry():
    """Create an email-channel inquiry for delivery tests.

    Returns:
        Inquiry: the created inquiry.
    """
    return Inquiry.objects.create(
        channel="email",
        cart_snapshot=SNAPSHOT,
        contact_hint="buyer@example.com",
    )


@override_settings(
    RESEND_API_KEY="re_test_key",
    ORDER_FROM_EMAIL="orders@example.com",
    ORDER_INTAKE_EMAIL="",
)
class OrderIntakeEmailServiceTests(APITestCase):
    """Exercises delivery success and every skip/failure branch."""

    def setUp(self):
        cache.clear()
        _set_intake_email("intake@example.com")

    @mock.patch("resend.Emails.send")
    def test_success_records_log_and_inquiry_state(self, mock_send):
        """A successful Resend call records the message id on both records."""
        mock_send.return_value = {"id": "email_123"}
        inquiry = _make_email_inquiry()

        log = send_order_intake_email(inquiry)

        mock_send.assert_called_once()
        params, options = mock_send.call_args.args
        self.assertEqual(params["to"], ["intake@example.com"])
        self.assertEqual(params["from"], "orders@example.com")
        self.assertIn(inquiry.reference, params["text"])
        self.assertEqual(
            options, {"idempotency_key": f"order-intake-inquiry-{inquiry.pk}"}
        )
        log.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.provider_message_id, "email_123")
        self.assertEqual(log.purpose, "order_intake")
        self.assertEqual(inquiry.email_status, "sent")
        self.assertEqual(inquiry.email_provider_message_id, "email_123")
        self.assertEqual(inquiry.email_attempts, 1)
        self.assertIsNotNone(inquiry.email_sent_at)
        self.assertEqual(inquiry.email_error, "")

    @mock.patch("resend.Emails.send")
    def test_api_error_is_logged_and_persisted(self, mock_send):
        """A provider error is logged, not swallowed, and marks both records."""
        from resend.exceptions import ResendError

        mock_send.side_effect = ResendError(
            code=500,
            error_type="application_error",
            message="Resend is unavailable",
            suggested_action="Try again later.",
        )
        inquiry = _make_email_inquiry()

        with self.assertLogs("apps.inquiries.email", level="ERROR") as captured:
            log = send_order_intake_email(inquiry)

        self.assertTrue(
            any("Resend order-intake email failed" in line for line in captured.output)
        )
        log.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(log.status, "failed")
        self.assertIn("Resend is unavailable", log.error_message)
        self.assertEqual(log.provider_response["code"], "500")
        self.assertEqual(inquiry.email_status, "failed")
        self.assertIn("Resend is unavailable", inquiry.email_error)
        self.assertEqual(inquiry.email_attempts, 1)

    @override_settings(RESEND_API_KEY="")
    @mock.patch("resend.Emails.send")
    def test_missing_api_key_is_recorded_without_provider_call(self, mock_send):
        """An unset key fails visibly instead of silently skipping delivery."""
        inquiry = _make_email_inquiry()

        log = send_order_intake_email(inquiry)

        mock_send.assert_not_called()
        log.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(log.status, "failed")
        self.assertIn("RESEND_API_KEY", log.error_message)
        self.assertEqual(inquiry.email_status, "failed")
        self.assertIn("RESEND_API_KEY", inquiry.email_error)

    @override_settings(ORDER_INTAKE_EMAIL="")
    @mock.patch("resend.Emails.send")
    def test_empty_intake_address_is_recorded_without_provider_call(self, mock_send):
        """Neither SiteConfig nor the env mailbox set means a visible failure."""
        _set_intake_email("")
        inquiry = _make_email_inquiry()

        log = send_order_intake_email(inquiry)

        mock_send.assert_not_called()
        log.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(log.status, "failed")
        self.assertIn("No order intake email", log.error_message)
        self.assertEqual(inquiry.email_status, "failed")
        self.assertIn("No order intake email", inquiry.email_error)

    @override_settings(ORDER_INTAKE_EMAIL="env-intake@example.com")
    @mock.patch("resend.Emails.send")
    def test_env_address_is_used_when_site_config_is_empty(self, mock_send):
        """The deployment mailbox is the fallback when SiteConfig is blank."""
        mock_send.return_value = {"id": "email_env"}
        _set_intake_email("")
        inquiry = _make_email_inquiry()

        send_order_intake_email(inquiry)

        params, _options = mock_send.call_args.args
        self.assertEqual(params["to"], ["env-intake@example.com"])

    @mock.patch("resend.Emails.send")
    def test_provider_response_never_contains_api_key(self, mock_send):
        """The stored payload, error text, and logs redact the secret key."""
        from resend.exceptions import ResendError

        mock_send.side_effect = ResendError(
            code=500,
            error_type="application_error",
            message="request rejected for re_test_key",
            suggested_action="",
        )
        inquiry = _make_email_inquiry()

        with self.assertLogs("apps.inquiries.email", level="ERROR") as captured:
            log = send_order_intake_email(inquiry)

        self.assertNotIn("re_test_key", log.error_message)
        self.assertNotIn("re_test_key", str(log.provider_response))
        self.assertNotIn("re_test_key", "".join(captured.output))

    @override_settings(ORDER_FROM_EMAIL="")
    @mock.patch("resend.Emails.send")
    def test_missing_sender_is_recorded_without_provider_call(self, mock_send):
        """An unset sender fails visibly before the provider is called."""
        inquiry = _make_email_inquiry()

        log = send_order_intake_email(inquiry)

        mock_send.assert_not_called()
        log.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(log.status, "failed")
        self.assertIn("ORDER_FROM_EMAIL", log.error_message)
        self.assertEqual(inquiry.email_status, "failed")
        self.assertIn("ORDER_FROM_EMAIL", inquiry.email_error)

    @mock.patch("resend.Emails.send")
    def test_empty_provider_response_is_recorded_as_failed(self, mock_send):
        """A provider response with no body is recorded instead of escaping."""
        from resend.exceptions import NoContentError

        mock_send.side_effect = NoContentError()
        inquiry = _make_email_inquiry()

        log = send_order_intake_email(inquiry)

        log.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual(log.status, "failed")
        self.assertIn("No content", log.error_message)
        self.assertEqual(inquiry.email_status, "failed")
        self.assertIn("No content", inquiry.email_error)

    @mock.patch("resend.Emails.send")
    def test_staff_resend_uses_a_fresh_idempotency_key(self, mock_send):
        """A deliberate resend is not suppressed by the automatic retry key."""
        mock_send.return_value = {"id": "email_resend"}
        inquiry = _make_email_inquiry()

        send_order_intake_email(inquiry)
        resend_order_intake_email(inquiry)

        first_options = mock_send.call_args_list[0].args[1]
        second_options = mock_send.call_args_list[1].args[1]
        self.assertEqual(
            first_options["idempotency_key"],
            f"order-intake-inquiry-{inquiry.pk}",
        )
        self.assertNotEqual(
            second_options["idempotency_key"],
            first_options["idempotency_key"],
        )
        self.assertIn("-resend-", second_options["idempotency_key"])

    def test_whatsapp_inquiry_is_rejected_by_the_email_service(self):
        """The service refuses a channel that has no mailbox copy."""
        inquiry = Inquiry.objects.create(channel="whatsapp", cart_snapshot=SNAPSHOT)

        with self.assertRaisesMessage(ValueError, "Only email inquiries"):
            send_order_intake_email(inquiry)

    @mock.patch("apps.inquiries.email.send_order_intake_email")
    def test_task_skips_redelivery_after_a_successful_send(self, mock_send):
        """A duplicate task run does not send an already-sent inquiry again."""
        inquiry = _make_email_inquiry()
        inquiry.email_status = "sent"
        inquiry.save(update_fields=["email_status"])

        result = send_order_intake_email_task.apply(args=[inquiry.pk]).get()

        mock_send.assert_not_called()
        self.assertEqual(result["status"], "already_sent")


class InquiryEmailViewTests(APITestCase):
    """Exercises the public capture wiring and staff resend endpoint."""

    def setUp(self):
        cache.clear()
        _set_intake_email("intake@example.com")

    @override_settings(
        RESEND_API_KEY="re_test_key",
        ORDER_FROM_EMAIL="orders@example.com",
    )
    @mock.patch("apps.inquiries.tasks.send_order_intake_email_task.delay")
    def test_email_capture_queues_delivery_after_commit(self, mock_delay):
        """An email inquiry queues the task and reports pending delivery."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                CREATE_URL,
                {"channel": "email", "cart_snapshot": SNAPSHOT},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inquiry = Inquiry.objects.get()
        mock_delay.assert_called_once_with(inquiry.pk)
        self.assertEqual(inquiry.email_status, "pending")
        self.assertEqual(response.data["email_status"], "pending")

    @mock.patch("apps.inquiries.tasks.send_order_intake_email_task.delay")
    def test_record_inquiry_queues_email_without_the_view(self, mock_delay):
        """The capture service owns queueing for every caller."""
        with self.captureOnCommitCallbacks(execute=True):
            inquiry = record_inquiry(channel="email", cart_snapshot=SNAPSHOT)

        mock_delay.assert_called_once_with(inquiry.pk)

    @mock.patch("apps.inquiries.tasks.send_order_intake_email_task.delay")
    def test_enqueue_failure_is_recorded_on_the_inquiry(self, mock_delay):
        """A broker failure is visible instead of leaving the row pending."""
        mock_delay.side_effect = RuntimeError("broker unavailable")

        with self.captureOnCommitCallbacks(execute=True):
            inquiry = record_inquiry(channel="email", cart_snapshot=SNAPSHOT)

        inquiry.refresh_from_db()
        self.assertEqual(inquiry.email_status, "failed")
        self.assertIn("Could not enqueue", inquiry.email_error)
        log = NotificationLog.objects.get()
        self.assertEqual(log.status, "failed")
        self.assertIn("Could not enqueue", log.error_message)

    @mock.patch("apps.inquiries.tasks.send_order_intake_email_task.delay")
    def test_whatsapp_capture_does_not_queue_email(self, mock_delay):
        """WhatsApp hand-offs stay in chat and do not send a mailbox copy."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                CREATE_URL,
                {"channel": "whatsapp", "cart_snapshot": SNAPSHOT},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_delay.assert_not_called()

    @override_settings(
        RESEND_API_KEY="re_test_key",
        ORDER_FROM_EMAIL="orders@example.com",
    )
    @mock.patch("resend.Emails.send")
    def test_staff_can_resend_and_sees_sent_state(self, mock_send):
        """A staff resend runs synchronously and updates the inquiry."""
        mock_send.return_value = {"id": "email_resend"}
        _make_staff()
        _login(self.client)
        inquiry = _make_email_inquiry()
        url = reverse(
            "api:inquiries:inquiry-resend-email",
            kwargs={"inquiry_id": inquiry.pk},
        )

        response = self.client.post(url, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email_status"], "sent")
        log = NotificationLog.objects.get()
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.purpose, "order_intake")
        self.assertEqual(log.sent_by.email, "staff@example.com")

    def test_anonymous_cannot_resend(self):
        """The resend action is staff-only."""
        inquiry = _make_email_inquiry()
        url = reverse(
            "api:inquiries:inquiry-resend-email",
            kwargs={"inquiry_id": inquiry.pk},
        )
        response = self.client.post(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_whatsapp_inquiry_cannot_be_resent(self):
        """There is no mailbox copy to resend for a WhatsApp inquiry."""
        _make_staff()
        _login(self.client)
        inquiry = Inquiry.objects.create(channel="whatsapp", cart_snapshot=SNAPSHOT)
        url = reverse(
            "api:inquiries:inquiry-resend-email",
            kwargs={"inquiry_id": inquiry.pk},
        )
        response = self.client.post(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_problem_filter_lists_pending_and_failed_email_inquiries(self):
        """The queue can narrow to email inquiries that need attention."""
        _make_staff()
        _login(self.client)
        pending = _make_email_inquiry()
        pending.email_status = "pending"
        pending.save(update_fields=["email_status"])
        failed = _make_email_inquiry()
        failed.email_status = "failed"
        failed.save(update_fields=["email_status"])
        Inquiry.objects.create(
            channel="email",
            cart_snapshot=SNAPSHOT,
            email_status="sent",
        )
        Inquiry.objects.create(channel="whatsapp", cart_snapshot=SNAPSHOT)

        response = self.client.get(
            reverse("api:inquiries:inquiry-queue"), {"email_status": "problem"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["id"] for row in response.data["results"]},
            {pending.pk, failed.pk},
        )


class InquiryEmailBackfillTests(APITestCase):
    """Exercises the historical delivery-state backfill."""

    def test_historical_email_inquiries_are_marked_failed(self):
        """Rows created before delivery tracking are flagged as unsent."""
        migration = importlib.import_module(
            "apps.inquiries.migrations."
            "0003_inquiry_email_attempts_inquiry_email_error_and_more"
        )
        email_inquiry = _make_email_inquiry()
        whatsapp_inquiry = Inquiry.objects.create(
            channel="whatsapp", cart_snapshot=SNAPSHOT
        )

        migration.mark_historical_email_inquiries_unsent(django_apps, None)

        email_inquiry.refresh_from_db()
        whatsapp_inquiry.refresh_from_db()
        self.assertEqual(email_inquiry.email_status, "failed")
        self.assertIn("No delivery attempt", email_inquiry.email_error)
        self.assertEqual(whatsapp_inquiry.email_status, "not_required")


class SendTestOrderEmailCommandTests(APITestCase):
    """Exercises the management command through the shared delivery path."""

    def setUp(self):
        cache.clear()

    @override_settings(
        RESEND_API_KEY="re_test_key",
        ORDER_FROM_EMAIL="orders@example.com",
        ORDER_INTAKE_EMAIL="ops@example.com",
    )
    @mock.patch("resend.Emails.send")
    def test_command_sends_and_prints_provider_id(self, mock_send):
        """The command sends a real test message and prints the result."""
        mock_send.return_value = {"id": "email_test_1"}
        out = StringIO()

        call_command("send_test_order_email", stdout=out)

        mock_send.assert_called_once()
        params, _options = mock_send.call_args.args
        self.assertEqual(params["to"], ["ops@example.com"])
        self.assertIn("email_test_1", out.getvalue())
        log = NotificationLog.objects.get()
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.purpose, "test")

    @override_settings(RESEND_API_KEY="", ORDER_INTAKE_EMAIL="ops@example.com")
    @mock.patch("resend.Emails.send")
    def test_command_reports_missing_key(self, mock_send):
        """A missing key prints the failure and leaves a failed audit row."""
        out = StringIO()
        err = StringIO()

        call_command("send_test_order_email", stdout=out, stderr=err)

        mock_send.assert_not_called()
        self.assertIn("RESEND_API_KEY", err.getvalue())
        self.assertEqual(NotificationLog.objects.get().status, "failed")
