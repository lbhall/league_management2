"""Copy a season out of an older database backup into this database.

Useful when a season was hard-archived (its weeks, matches and player
results deleted) but an earlier backup still has the live data. Teams and
players are matched to the current league by name; players missing from
the current league are created. Nothing in the backup is modified.

Dry run (reports what would be created, changes nothing):
    python manage.py restore_season_from_backup --db /path/to/backup.sqlite3 \\
        --league "EMC Fun Pool League" --season-id 12
Apply, making it the active season:
    python manage.py restore_season_from_backup --db /path/to/backup.sqlite3 \\
        --league "EMC Fun Pool League" --season-id 12 --status active --apply
"""
import sqlite3
from datetime import date, datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import League, Player, Team
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season, Week


class Command(BaseCommand):
    help = 'Copy a season (weeks, matches, results, player scores) from an older SQLite backup.'

    def add_arguments(self, parser):
        parser.add_argument('--db', required=True, help='Path to the backup SQLite file')
        parser.add_argument('--league', required=True, help='League name in this database')
        parser.add_argument('--season-id', type=int, required=True, help='Season id inside the backup')
        parser.add_argument('--name', default=None, help='Name for the restored season (default: same as backup)')
        parser.add_argument('--status', choices=['working', 'active', 'archived'], default='working',
                            help='Status for the restored season (default: working)')
        parser.add_argument('--apply', action='store_true',
                            help='Make the change. Without this flag the command only reports.')

    def handle(self, *args, **options):
        try:
            league = League.objects.get(name=options['league'])
        except League.DoesNotExist:
            raise CommandError(f'League "{options["league"]}" not found.')

        try:
            conn = sqlite3.connect(f'file:{options["db"]}?mode=ro', uri=True)
        except sqlite3.Error as exc:
            raise CommandError(f'Could not open backup: {exc}')
        conn.row_factory = sqlite3.Row
        try:
            self._restore(conn, league, options)
        finally:
            conn.close()

    def _restore(self, conn, league, options):
        old_season = conn.execute(
            'SELECT id, name, status, league_id FROM scheduling_season WHERE id = ?', (options['season_id'],)
        ).fetchone()
        if old_season is None:
            raise CommandError(f'Season {options["season_id"]} not found in the backup.')

        name = options['name'] or old_season['name']
        status = options['status']
        if Season.objects.filter(league=league, name=name).exists():
            raise CommandError(f'League "{league.name}" already has a season named "{name}".')
        if status == 'active' and Season.objects.filter(league=league, status=Season.Status.ACTIVE).exists():
            raise CommandError(f'League "{league.name}" already has an active season.')

        old_weeks = conn.execute(
            'SELECT id, date, number, notes FROM scheduling_week WHERE season_id = ? ORDER BY date, number',
            (old_season['id'],),
        ).fetchall()
        week_ids = [w['id'] for w in old_weeks]
        if not week_ids:
            raise CommandError('That season has no weeks in the backup.')
        marks = ','.join('?' * len(week_ids))
        old_matches = conn.execute(
            f'SELECT id, week_id, home_team_id, away_team_id, location, sort_order '
            f'FROM scheduling_match WHERE week_id IN ({marks}) ORDER BY week_id, sort_order, id',
            week_ids,
        ).fetchall()
        match_ids = [m['id'] for m in old_matches]
        old_results = []
        old_scores = []
        if match_ids:
            marks = ','.join('?' * len(match_ids))
            old_results = conn.execute(
                f'SELECT id, match_id, home_team_score, away_team_score, created_at, updated_at '
                f'FROM results_matchresult WHERE match_id IN ({marks})',
                match_ids,
            ).fetchall()
            result_ids = [r['id'] for r in old_results]
            if result_ids:
                marks = ','.join('?' * len(result_ids))
                old_scores = conn.execute(
                    f'SELECT match_result_id, player_id, represented_team_id, wins, losses, runouts, '
                    f'eight_on_the_breaks, won_all_games '
                    f'FROM results_playermatchresult WHERE match_result_id IN ({marks})',
                    result_ids,
                ).fetchall()

        # --- map teams and players by name -----------------------------------
        old_teams = {
            r['id']: r['name']
            for r in conn.execute('SELECT id, name FROM core_team WHERE league_id = ?', (old_season['league_id'],))
        }
        old_players = {
            r['id']: r
            for r in conn.execute(
                'SELECT id, name, phone, male, team_id FROM core_player WHERE league_id = ?', (old_season['league_id'],)
            )
        }
        live_teams = {t.name: t for t in Team.objects.filter(league=league)}
        live_players = {}
        for p in Player.objects.filter(league=league).order_by('id'):
            live_players.setdefault(p.name, p)

        used_team_ids = {m['home_team_id'] for m in old_matches} | {m['away_team_id'] for m in old_matches}
        used_team_ids |= {s['represented_team_id'] for s in old_scores}
        missing_teams = sorted(old_teams.get(tid, f'id {tid}') for tid in used_team_ids if old_teams.get(tid) not in live_teams)
        if missing_teams:
            raise CommandError(f'Teams in the backup have no match in "{league.name}": {", ".join(missing_teams)}')

        used_player_ids = {s['player_id'] for s in old_scores}
        new_players = sorted(
            old_players[pid]['name'] for pid in used_player_ids
            if pid in old_players and old_players[pid]['name'] not in live_players
        )
        unknown_players = [pid for pid in used_player_ids if pid not in old_players]
        if unknown_players:
            raise CommandError(f'Player ids missing from the backup: {unknown_players}')

        self.stdout.write(f'Backup season {old_season["id"]} "{old_season["name"]}" ({old_season["status"]})')
        self.stdout.write(
            f'  -> "{name}" ({status}) in "{league.name}": {len(old_weeks)} weeks, {len(old_matches)} matches, '
            f'{len(old_results)} results, {len(old_scores)} player scores'
        )
        if new_players:
            self.stdout.write(f'  players to create: {", ".join(new_players)}')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing changed. Re-run with --apply to restore.'))
            return

        with transaction.atomic():
            season = Season.objects.create(league=league, name=name, status=status)

            for pid in used_player_ids:
                row = old_players[pid]
                if row['name'] not in live_players:
                    team_name = old_teams.get(row['team_id'])
                    live_players[row['name']] = Player.objects.create(
                        league=league,
                        name=row['name'],
                        phone=row['phone'] or '',
                        male=bool(row['male']),
                        team=live_teams.get(team_name),
                    )

            week_map = {}
            for w in old_weeks:
                week_map[w['id']] = Week.objects.create(
                    season=season, date=date.fromisoformat(w['date']), number=w['number'], notes=w['notes'] or '',
                )

            match_map = {}
            for m in old_matches:
                match_map[m['id']] = Match.objects.create(
                    week=week_map[m['week_id']],
                    home_team=live_teams[old_teams[m['home_team_id']]],
                    away_team=live_teams[old_teams[m['away_team_id']]],
                    location=m['location'] or '',
                    sort_order=m['sort_order'] or 0,
                )

            result_map = {}
            for r in old_results:
                result = MatchResult.objects.create(
                    match=match_map[r['match_id']],
                    home_team_score=r['home_team_score'],
                    away_team_score=r['away_team_score'],
                )
                # Keep the original entry timestamps rather than "now".
                stamps = {}
                for field in ('created_at', 'updated_at'):
                    if r[field]:
                        stamps[field] = _parse_timestamp(r[field])
                if stamps:
                    MatchResult.objects.filter(pk=result.pk).update(**stamps)
                result_map[r['id']] = result

            for s in old_scores:
                PlayerMatchResult.objects.create(
                    match_result=result_map[s['match_result_id']],
                    player=live_players[old_players[s['player_id']]['name']],
                    represented_team=live_teams[old_teams[s['represented_team_id']]],
                    wins=s['wins'],
                    losses=s['losses'],
                    runouts=s['runouts'],
                    eight_on_the_breaks=s['eight_on_the_breaks'],
                    won_all_games=bool(s['won_all_games']),
                )

        self.stdout.write(self.style.SUCCESS(
            f'Restored "{name}" as season {season.pk}: {len(week_map)} weeks, {len(match_map)} matches, '
            f'{len(result_map)} results, {len(old_scores)} player scores, {len(new_players)} new players.'
        ))


def _parse_timestamp(value):
    """SQLite stores Django datetimes as ISO text; accept with or without microseconds/offset."""
    text = str(value).replace(' ', 'T')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
