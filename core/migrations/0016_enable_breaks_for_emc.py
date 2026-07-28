from django.db import migrations

LEAGUE_NAME = "EMC Fun Pool League"


def enable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(show_breaks=True)


def disable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(show_breaks=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_league_show_breaks"),
    ]

    operations = [
        migrations.RunPython(enable_for_emc, disable_for_emc),
    ]
