# The warehouse-routing model this migration used to alter was removed with
# the warehouse-tracking retirement (its creation was dropped from 0001), so
# this migration is intentionally a no-op. The file is kept so the existing
# 0003 -> 0004 chain still resolves on databases that already applied it.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0001_initial"),
    ]

    operations = []
