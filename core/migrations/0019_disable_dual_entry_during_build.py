from django.db import migrations

LEAGUE_NAME = "EMC Fun Pool League"


def disable_for_emc(apps, schema_editor):
    # Dual-entry is built across several phases. Keep it off in production while
    # the pipeline is incomplete (captain capture lands before reconciliation /
    # finalize); a later migration re-enables it once the full flow is live.
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(dual_entry_scoring=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_enable_dual_entry_for_emc"),
    ]

    operations = [
        migrations.RunPython(disable_for_emc, noop),
    ]
