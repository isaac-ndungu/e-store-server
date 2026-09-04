from django.apps import AppConfig


class OrdersConfig(AppConfig):
    """App configuration for the orders app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"

    def ready(self):
        """Import the orders signal receivers so they register on startup."""
        import apps.orders.signals  # noqa: F401
