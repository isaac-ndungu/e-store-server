"""Send a real test email through the order-intake delivery path."""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.inquiries.email import send_order_intake_test_email


class Command(BaseCommand):
    """Send one test order-intake email and print the provider result."""

    help = (
        "Send a real test email to ORDER_INTAKE_EMAIL through the same Resend "
        "path used for storefront inquiries, and print the result."
    )

    def handle(self, *args, **options):
        """Run the test send.

        Args:
            *args: unused positional arguments.
            **options: unused command options.
        """
        recipient = (getattr(settings, "ORDER_INTAKE_EMAIL", "") or "").strip()
        if not recipient:
            self.stderr.write(
                self.style.ERROR("ORDER_INTAKE_EMAIL is empty; no test email was sent.")
            )

        log = send_order_intake_test_email()
        if log.status == "sent":
            self.stdout.write(
                self.style.SUCCESS(
                    f"Test email sent to {recipient} (provider id: {log.provider_message_id})."
                )
            )
            return

        self.stderr.write(
            self.style.ERROR(
                f"Test email failed (notification log {log.pk}): {log.error_message}"
            )
        )
