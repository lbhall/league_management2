from django.db import migrations


def backfill(apps, schema_editor):
    """Freeze each side's current rank onto existing one-pocket results so a
    later rank change can't relabel matches already played.

    Only where the result is credited to the spot's current holder (or has no
    player recorded): a match played by someone who has since left the spot
    may have been at a different rank, and that is set by hand through
    "Record a player change" rather than guessed here.
    """
    MatchResult = apps.get_model('results', 'MatchResult')
    Player = apps.get_model('core', 'Player')

    def holder_id(team_id):
        player = Player.objects.filter(team_id=team_id).order_by('id').first()
        return player.id if player else None

    results = (
        MatchResult.objects.filter(match__week__season__league__results_type='one_pocket')
        .select_related('match__home_team', 'match__away_team')
    )
    for result in results:
        changed = []
        home = result.match.home_team
        away = result.match.away_team
        if result.home_rank is None and home.team_rank is not None:
            if result.home_player_id in (None, holder_id(home.id)):
                result.home_rank = home.team_rank
                changed.append('home_rank')
        if result.away_rank is None and away.team_rank is not None:
            if result.away_player_id in (None, holder_id(away.id)):
                result.away_rank = away.team_rank
                changed.append('away_rank')
        if changed:
            result.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ('results', '0007_matchresult_ranks'),
        ('core', '0024_pageview_verbose_name'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
