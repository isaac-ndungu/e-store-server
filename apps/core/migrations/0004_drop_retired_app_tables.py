# Drops the tables of the retired warehouse-tracking and mobile-money
# payout apps on databases that already applied their migrations. Fresh
# databases never create these tables (the apps no longer exist), and
# ``IF EXISTS`` keeps this a no-op there.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_siteconfig_order_intake_email_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DROP TABLE IF EXISTS inventory_stockmovementlog;
            DROP TABLE IF EXISTS inventory_serialunit;
            DROP TABLE IF EXISTS inventory_inventory;
            DROP TABLE IF EXISTS inventory_warehouse;
            DROP TABLE IF EXISTS payments_mpesab2cpayout;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
