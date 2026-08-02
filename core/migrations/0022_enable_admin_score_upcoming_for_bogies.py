from django.db import migrations


def enable_for_bogies(apps, schema_editor):
    # Match the Bogies one-pocket league the same way the import commands do
    # (its exact name varies), so admins can score its upcoming matches.
    League = apps.get_model("core", "League")
    League.objects.filter(
        name__icontains="Bogies", results_type="one_pocket",
    ).update(allow_admin_score_upcoming=True)


def disable_for_bogies(apps, schema_editor):
    League = apps.get_model("core", "League")
    League.objects.filter(
        name__icontains="Bogies", results_type="one_pocket",
    ).update(allow_admin_score_upcoming=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_league_allow_admin_score_upcoming"),
    ]

    operations = [
        migrations.RunPython(enable_for_bogies, disable_for_bogies),
    ]
