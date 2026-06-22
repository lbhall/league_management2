import re
import sqlite3
from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import League
from scheduling.models import ArchivedSeason, ArchivedTeam


RANK_SUFFIX_RE = re.compile(r'\s*\(\d+\)\s*$')
PERIOD_END_RE = re.compile(r'-\s*(\d{1,2}/\d{1,2}/\d{4})\s*$')


def strip_rank_suffix(name):
    return RANK_SUFFIX_RE.sub('', name).strip()


def parse_period_end(period):
    match = PERIOD_END_RE.search(period)
    if not match:
        return None
    try:
        naive = datetime.combine(
            datetime.strptime(match.group(1), '%m/%d/%Y').date(),
            time(12, 0),
        )
    except ValueError:
        return None
    return timezone.make_aware(naive, timezone.get_current_timezone())


class Command(BaseCommand):
    help = 'Import archived seasons + player standings from the legacy Bogies SQLite database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--db-path',
            default='database_backups/bogies/20260608-110817.db.sqlite3',
            help='Path to the legacy SQLite database (default: %(default)s).',
        )
        parser.add_argument(
            '--league-id',
            type=int,
            default=None,
            help='Target league ID. If omitted, picks the first Bogies one-pocket league.',
        )

    def handle(self, *args, **options):
        db_path = options['db_path']
        league_id = options['league_id']

        if league_id:
            league = League.objects.filter(pk=league_id).first()
            if not league:
                self.stderr.write(f'No league with id={league_id}')
                return
        else:
            league = (
                League.objects
                .filter(name__icontains='Bogies', results_type='one_pocket')
                .first()
            )
            if not league:
                self.stderr.write(
                    'Could not find a Bogies one-pocket league. Pass --league-id explicitly.'
                )
                return

        self.stdout.write(f'Importing into league: {league.name} (id={league.id}) from {db_path}')

        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.OperationalError as e:
            self.stderr.write(f'Could not open database at {db_path}: {e}')
            return

        cur = conn.cursor()

        seasons_created = 0
        seasons_existing = 0
        teams_created = 0
        teams_updated = 0

        try:
            with transaction.atomic():
                cur.execute('SELECT id, period FROM league_archiveseason ORDER BY id')
                legacy_seasons = cur.fetchall()

                for legacy_id, period in legacy_seasons:
                    archived_season, created = ArchivedSeason.objects.get_or_create(
                        league=league,
                        name=period,
                    )
                    if created:
                        seasons_created += 1
                        backdated = parse_period_end(period)
                        if backdated:
                            ArchivedSeason.objects.filter(pk=archived_season.pk).update(
                                archived_at=backdated
                            )
                        self.stdout.write(f'  + season created: {period}')
                    else:
                        seasons_existing += 1
                        self.stdout.write(f'  = season already existed: {period}')

                    cur.execute(
                        'SELECT name, matches_won, matches_lost, games_won, games_lost '
                        'FROM league_playerarchive WHERE season_id = ? ORDER BY rank',
                        (legacy_id,),
                    )
                    for raw_name, mw, ml, gw, gl in cur.fetchall():
                        clean_name = strip_rank_suffix(raw_name)
                        if not clean_name:
                            self.stderr.write(f'  ! skipping empty name from {raw_name!r}')
                            continue

                        team, t_created = ArchivedTeam.objects.get_or_create(
                            archived_season=archived_season,
                            team_name=clean_name,
                            defaults={
                                'matches_won': mw,
                                'matches_lost': ml,
                                'games_won': gw,
                                'games_lost': gl,
                            },
                        )
                        if t_created:
                            teams_created += 1
                            continue

                        update_fields = []
                        for field, val in (
                            ('matches_won', mw),
                            ('matches_lost', ml),
                            ('games_won', gw),
                            ('games_lost', gl),
                        ):
                            if getattr(team, field) != val:
                                setattr(team, field, val)
                                update_fields.append(field)
                        if update_fields:
                            team.save(update_fields=update_fields)
                            teams_updated += 1
        finally:
            conn.close()

        self.stdout.write(self.style.SUCCESS(
            f'Done. Seasons: {seasons_created} created, {seasons_existing} existed. '
            f'Players: {teams_created} created, {teams_updated} updated.'
        ))
