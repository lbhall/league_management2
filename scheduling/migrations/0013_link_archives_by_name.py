from django.db import migrations


def _unique_by_name(queryset):
    by_name = {}
    for obj in queryset:
        by_name.setdefault(obj.name.strip().lower(), []).append(obj)
    return {key: objs[0] for key, objs in by_name.items() if len(objs) == 1}


def link_by_name(apps, schema_editor):
    """Best-effort backfill: point archived rows at the live team/player with
    the same name in the same league. Ambiguous or missing names stay null;
    the admin action "Link archived teams/players to current records by name"
    (or manual editing) can finish the job later.
    """
    ArchivedSeason = apps.get_model('scheduling', 'ArchivedSeason')
    ArchivedPlayerMatchResult = apps.get_model('scheduling', 'ArchivedPlayerMatchResult')
    Team = apps.get_model('core', 'Team')
    Player = apps.get_model('core', 'Player')

    for archived_season in ArchivedSeason.objects.all():
        league_id = archived_season.league_id
        teams = _unique_by_name(Team.objects.filter(league_id=league_id))
        players = _unique_by_name(Player.objects.filter(league_id=league_id))

        for row in archived_season.teams.filter(team__isnull=True):
            team = teams.get(row.team_name.strip().lower())
            if team:
                row.team = team
                row.save(update_fields=['team'])

        for row in archived_season.players.filter(player__isnull=True):
            player = players.get(row.player_name.strip().lower())
            if player:
                row.player = player
                row.save(update_fields=['player'])

        for row in ArchivedPlayerMatchResult.objects.filter(
            archived_match__archived_season=archived_season,
            player__isnull=True,
        ):
            player = players.get(row.player_name.strip().lower())
            if player:
                row.player = player
                row.save(update_fields=['player'])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0012_archive_player_links'),
    ]

    operations = [
        migrations.RunPython(link_by_name, migrations.RunPython.noop),
    ]
