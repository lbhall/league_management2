from django.db import migrations


def backfill(apps, schema_editor):
    """Credit every existing one-pocket result to whoever currently holds
    each spot. Where a spot changed hands earlier, the admin's "Record a
    player change" action re-credits the earlier matches afterwards."""
    MatchResult = apps.get_model('results', 'MatchResult')
    Player = apps.get_model('core', 'Player')

    results = (
        MatchResult.objects.filter(match__week__season__league__results_type='one_pocket')
        .select_related('match')
    )
    occupant_cache = {}

    def occupant(team_id):
        if team_id not in occupant_cache:
            occupant_cache[team_id] = Player.objects.filter(team_id=team_id).order_by('id').first()
        return occupant_cache[team_id]

    for result in results:
        changed = []
        if result.home_player_id is None:
            player = occupant(result.match.home_team_id)
            if player is not None:
                result.home_player = player
                changed.append('home_player')
        if result.away_player_id is None:
            player = occupant(result.match.away_team_id)
            if player is not None:
                result.away_player = player
                changed.append('away_player')
        if changed:
            result.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ('results', '0005_matchresult_players'),
        ('core', '0024_pageview_verbose_name'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
