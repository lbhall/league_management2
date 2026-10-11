from datetime import datetime

from django.db import migrations
from django.db.models import Q


DATE_FORMATS = ('%Y-%m-%d', '%m/%d/%Y')


def _parse_date(text):
    text = (text or '').strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def backfill(apps, schema_editor):
    ArchivedSeason = apps.get_model('scheduling', 'ArchivedSeason')
    ArchivedMatch = apps.get_model('scheduling', 'ArchivedMatch')

    for season in ArchivedSeason.objects.select_related('league').prefetch_related('teams'):
        season.results_type = season.league.results_type
        season.team_size = season.league.team_size

        parts = season.name.split(' - ', 1)
        if len(parts) == 2:
            season.start_date = _parse_date(parts[0])
            season.end_date = _parse_date(parts[1])

        season.save(update_fields=['results_type', 'team_size', 'start_date', 'end_date'])

        ranked = sorted(
            season.teams.all(),
            key=lambda t: (-t.matches_won, t.matches_lost, -t.games_won, t.games_lost, t.team_name),
        )
        for position, team in enumerate(ranked, start=1):
            team.league_rank = position
            team.save(update_fields=['league_rank'])

    scored = Q(home_team_score__isnull=False, away_team_score__isnull=False)
    ArchivedMatch.objects.filter(scored).exclude(
        archived_season__results_type='one_pocket',
    ).update(completed=True)
    ArchivedMatch.objects.filter(scored, archived_season__results_type='one_pocket').filter(
        Q(home_team_score__gte=3) | Q(away_team_score__gte=3),
    ).update(completed=True)


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0009_archive_snapshot_fields'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
