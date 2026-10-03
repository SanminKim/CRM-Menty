from django.core.management import call_command
from django.db import migrations, models


def create_cache_table(apps, schema_editor):
    # Таблица счётчика неудачных входов: создаётся вместе с базой, отдельная команда не нужна
    call_command("createcachetable", database=schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [("backend", "0001_initial")]

    operations = [
        migrations.AddIndex(
            model_name="tombstone",
            index=models.Index(fields=["collection", "doc_id"], name="tombstone_doc"),
        ),
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
