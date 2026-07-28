from django.db import migrations

LEAGUE_NAME = "EMC Fun Pool League"


def enable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(allow_game_winner_clear=True)


def disable_for_emc(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(name=LEAGUE_NAME).update(allow_game_winner_clear=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_league_allow_game_winner_clear"),
    ]

    operations = [
        migrations.RunPython(enable_for_emc, disable_for_emc),
    ]
