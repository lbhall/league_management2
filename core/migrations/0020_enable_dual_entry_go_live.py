from django.db import migrations

LEAGUE_NAME = "EMC Fun Pool League"


def enable_for_emc(apps, schema_editor):
    # Dual-entry scoring is complete (phases 1-4 + import/live checks) and
    # validated on beta. Turn it on for the EMC Fun Pool League.
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(dual_entry_scoring=True)


def disable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(dual_entry_scoring=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_disable_dual_entry_during_build"),
    ]

    operations = [
        migrations.RunPython(enable_for_emc, disable_for_emc),
    ]
