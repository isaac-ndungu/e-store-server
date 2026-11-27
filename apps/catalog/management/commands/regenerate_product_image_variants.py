"""Queue regeneration of responsive product image variants."""

from django.core.management.base import BaseCommand

from apps.catalog.models import ProductImage
from apps.catalog.tasks import generate_product_image_variants


class Command(BaseCommand):
    """Queue product image variant regeneration through Celery."""

    help = "Queue responsive image variant regeneration for product images."

    def add_arguments(self, parser):
        """Add optional product filtering and dry-run flags."""
        parser.add_argument(
            "--product-id",
            type=int,
            help="Regenerate images belonging to this product only.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the number of images without queueing tasks.",
        )

    def handle(self, *args, **options):
        """Queue idempotent regeneration tasks for matching product images."""
        images = (
            ProductImage.objects.exclude(image="").only("pk", "image").order_by("pk")
        )
        if options["product_id"] is not None:
            images = images.filter(product_id=options["product_id"])

        queued = 0
        for product_image in images.iterator(chunk_size=200):
            queued += 1
            if not options["dry_run"]:
                generate_product_image_variants.delay(
                    product_image.pk,
                    product_image.image.name,
                    force=True,
                )

        if options["dry_run"]:
            self.stdout.write(
                f"Would queue variant regeneration for {queued} image(s)."
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Queued variant regeneration for {queued} image(s)."
                )
            )
