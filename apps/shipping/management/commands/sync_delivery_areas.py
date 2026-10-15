"""Populate the delivery-area list from the courier pickup network.

Loads the PII-free county + town snapshot shipped under
``apps/shipping/data/`` and upserts one ``DeliveryArea`` per pair. The
command is idempotent: running it twice creates no duplicates, and
deactivated areas that reappear in the feed are switched back on unless
``--no-reactivate`` is passed.
"""

import json
import urllib.request
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.shipping.services import sync_delivery_areas

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "escrow_pickup_areas.json"
)
FEED_URL = "https://escrowcourier.com/api/pickup-points"


class Command(BaseCommand):
    """Sync ``DeliveryArea`` rows from the bundled pickup-point snapshot."""

    help = "Populate DeliveryArea rows from the courier pickup-point list."

    def add_arguments(self, parser):
        """Declare the optional live-feed and reactivation flags.

        Args:
            parser: the argument parser for this command.
        """
        parser.add_argument(
            "--from-api",
            action="store_true",
            help="Fetch the live pickup-point feed instead of the bundled file.",
        )
        parser.add_argument(
            "--no-reactivate",
            action="store_true",
            help="Leave already-deactivated areas switched off.",
        )

    def handle(self, *args, **options):
        """Load pairs from the fixture or live feed and upsert them.

        Args:
            *args: unused positional arguments.
            **options: ``from_api`` and ``no_reactivate`` flags.

        Raises:
            CommandError: when the fixture or feed cannot be read.
        """
        try:
            pairs = self._load_pairs(from_api=options["from_api"])
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f"Could not load pickup-point pairs: {exc}") from exc
        result = sync_delivery_areas(pairs, reactivate=not options["no_reactivate"])
        self.stdout.write(
            self.style.SUCCESS(
                "Delivery areas synced: "
                f"{result['created']} created, "
                f"{result['reactivated']} reactivated, "
                f"{result['skipped']} skipped."
            )
        )

    def _load_pairs(self, from_api=False):
        """Return raw ``(county, town)`` pairs from the fixture or live feed.

        Only county and town are read; agent names, addresses, contacts, and
        coordinates from the courier feed are intentionally dropped.

        Args:
            from_api (bool): when True, fetch the live JSON feed.

        Returns:
            list[tuple[str, str]]: the raw county + town pairs.
        """
        if from_api:
            return self._load_from_api()
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        return [(row["county"], row["area_name"]) for row in payload]

    def _load_from_api(self):
        """Fetch raw ``(county, town)`` pairs from the live courier feed.

        Returns:
            list[tuple[str, str]]: the raw county + town pairs.
        """
        request = urllib.request.Request(
            FEED_URL,
            headers={"Accept": "application/json", "User-Agent": "estore-sync/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload["data"] if isinstance(payload, dict) else payload
        return [(row.get("county", ""), row.get("town", "")) for row in rows]
