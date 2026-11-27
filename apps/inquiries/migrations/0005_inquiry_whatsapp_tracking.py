# Generated manually for inquiry deduplication, short reference codes, and WhatsApp statuses.

import secrets

from django.db import migrations, models

_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def backfill_reference_codes(apps, schema_editor):
    """Populate unique VF-XXXX reference codes for existing inquiry rows."""
    Inquiry = apps.get_model("inquiries", "Inquiry")
    seen = set(
        Inquiry.objects.exclude(reference_code__isnull=True)
        .exclude(reference_code="")
        .values_list("reference_code", flat=True)
    )
    for inquiry in Inquiry.objects.filter(reference_code__isnull=True).iterator():
        code = None
        for _ in range(30):
            candidate = "VF-" + "".join(secrets.choice(_ALPHABET) for _ in range(4))
            if candidate not in seen:
                seen.add(candidate)
                code = candidate
                break
        if code is None:
            code = "VF-" + "".join(secrets.choice(_ALPHABET) for _ in range(8))
            seen.add(code)
        inquiry.reference_code = code
        inquiry.save(update_fields=["reference_code"])


class Migration(migrations.Migration):

    dependencies = [
        ("inquiries", "0004_inquiry_customer_email_inquiry_customer_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="inquiry",
            name="idempotency_key",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Client-supplied idempotency key for tap deduplication.",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="inquiry",
            name="message_received_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When staff confirmed the customer's chat message arrived.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="inquiry",
            name="page_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Storefront page URL where the visitor initiated the hand-off.",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="inquiry",
            name="recovery_token",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Opaque token authorizing post-tap recovery contact updates.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="inquiry",
            name="reference_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Short unique code (e.g. VF-4K8P) embedded in hand-off messages.",
                max_length=16,
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="inquiry",
            name="status",
            field=models.CharField(
                choices=[
                    ("clicked", "Clicked"),
                    ("message_received", "Message received"),
                    ("new", "New"),
                    ("contacted", "Contacted"),
                    ("converted", "Converted"),
                    ("abandoned", "Abandoned"),
                    ("spam", "Spam"),
                ],
                default="clicked",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_reference_codes, migrations.RunPython.noop),
    ]
