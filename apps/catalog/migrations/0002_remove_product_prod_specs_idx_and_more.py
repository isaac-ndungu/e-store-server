import django.contrib.postgres.indexes
from django.db import migrations, models


class AddPostgresGinIndex(migrations.AddIndex):
    """Create a GIN index only on PostgreSQL.

    The SQLite-backed test database cannot create GIN indexes, so the index
    is applied only when the schema editor targets PostgreSQL. Model state
    is updated on every backend so autodetection stays in sync.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="product",
            name="prod_specs_idx",
        ),
        AddPostgresGinIndex(
            model_name="product",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["specs"],
                name="prod_specs_gin_idx",
                opclasses=["jsonb_path_ops"],
            ),
        ),
        AddPostgresGinIndex(
            model_name="productvariant",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["attributes"],
                name="var_attrs_gin_idx",
                opclasses=["jsonb_path_ops"],
            ),
        ),
        migrations.AddConstraint(
            model_name="productimage",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True)),
                fields=("product",),
                name="unique_primary_image_per_product",
            ),
        ),
    ]
