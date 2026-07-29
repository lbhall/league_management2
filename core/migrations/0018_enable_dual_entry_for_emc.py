from django.db import migrations

LEAGUE_NAME = "EMC Fun Pool League"


def enable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(dual_entry_scoring=True)


def disable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(dual_entry_scoring=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_league_dual_entry_scoring"),
    ]

    operations = [
        migrations.RunPython(enable_for_emc, disable_for_emc),
    ]
