"""Publish the canonical content pages to the storefront.

Creates the canonical pages from ``apps.content.policy_texts`` when their
slugs are missing. Existing rows are left untouched unless ``--update`` is
passed, so staff edits made through the console are never silently
overwritten. Safe to run repeatedly; publishes nothing when everything
already exists.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.content.models import ContentPage
from apps.content.policy_texts import POLICY_PAGES, POLICY_VERSION
from apps.content.services import create_page, update_page


class Command(BaseCommand):
    """Seed (and optionally refresh) the published canonical pages."""

    help = "Create missing policy pages from the canonical texts."

    def add_arguments(self, parser):
        """Register the ``--update`` refresh flag."""
        parser.add_argument(
            "--update",
            action="store_true",
            help="Refresh the body and metadata of existing policy pages.",
        )

    def handle(self, *args, **options):
        """Create missing pages, refreshing existing ones only with --update.

        Args:
            *args: unused positional arguments.
            **options: command options, honouring ``update``.
        """
        refresh = bool(options.get("update"))
        created = 0
        refreshed = 0
        with transaction.atomic():
            for entry in POLICY_PAGES:
                page = ContentPage.objects.filter(slug=entry["slug"]).first()
                if page is None:
                    create_page(
                        title=entry["title"],
                        slug=entry["slug"],
                        body=entry["body"],
                        is_published=True,
                        meta_title=entry["meta_title"],
                        meta_description=entry["meta_description"],
                    )
                    created += 1
                elif refresh:
                    update_page(
                        page=page,
                        title=entry["title"],
                        body=entry["body"],
                        meta_title=entry["meta_title"],
                        meta_description=entry["meta_description"],
                    )
                    refreshed += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Policy pages v{POLICY_VERSION}: "
                f"{created} created, {refreshed} refreshed."
            )
        )
