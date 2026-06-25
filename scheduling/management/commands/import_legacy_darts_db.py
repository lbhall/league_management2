"""
Management command to import the current season from a legacy "runner_*"
darts league SQLite database (a different legacy schema than the
league_*-prefixed EMC pool DB handled by import_legacy_db).

Imports:
  - Venues (runner_bar)
  - Teams (runner_team) + seeds (runner_seed)
  - Players (runner_player)
  - Current season/weeks/matches (runner_schedule, runner_week, runner_match)
  - Match-level scores (runner_matchworkingscore -> MatchResult.home/away_team_score)
  - Per-player dart stats (runner_matchworkingscore's home/away_player_{1,2}_*
    columns -> PlayerMatchResult.hat_tricks/three_in_a_beds/white_horses/
    three_in_the_blacks)

Does NOT import runner_playerscore (a separate legacy table keyed by
date+player+team rather than match id) -- the per-player stats embedded
directly in runner_matchworkingscore above are the authoritative per-match
source and are sufficient to reconstruct the Top Players leaderboard.

Note: MatchResult.clean() normally rejects leagues that aren't 8-ball or
one-pocket, but that validation only runs through ModelForm/full_clean(), not
on save(), so creating MatchResult rows directly here for a darts league is
safe at the DB level even though the admin UI has no darts score-entry form yet.

Usage:
  python manage.py import_legacy_darts_db \\
      --db /path/to/darts.db.sqlite3 \\
      --league "COED Dart League" \\
      [--replace-active] \\
      [--skip-scores] \\
      [--dry-run]
"""

import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import League, Venue, Team, Player
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season, Week


class Command(BaseCommand):
    help = 'Import venues, teams, players, and the current schedule from a legacy darts SQLite database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--db',
            required=True,
            help='Path to the legacy darts SQLite database file.',
        )
        parser.add_argument(
            '--league',
            required=True,
            help='Name of the target league to import data into.',
        )
        parser.add_argument(
            '--replace-active',
            action='store_true',
            help=(
                'If an active season already exists for the league, delete it '
                '(and its weeks/matches/results) before importing the new one.'
            ),
        )
        parser.add_argument(
            '--skip-scores',
            action='store_true',
            help='Skip importing match-level scores.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate the import without writing anything to the database.',
        )

    def handle(self, *args, **options):
        db_path = Path(options['db']).expanduser()
        league_name = options['league']
        dry_run = options['dry_run']
        replace_active = options['replace_active']
        skip_scores = options['skip_scores']

        self.stdout.write(self.style.NOTICE(f'Legacy DB      : {db_path}'))
        self.stdout.write(self.style.NOTICE(f'League         : {league_name}'))
        self.stdout.write(self.style.NOTICE(f'Replace active : {"yes" if replace_active else "no"}'))
        self.stdout.write(self.style.NOTICE(f'Dry run        : {"yes" if dry_run else "no"}'))

        if not db_path.exists():
            raise CommandError(f'Database file not found: {db_path}')

        try:
            league = League.objects.get(name=league_name)
        except League.DoesNotExist:
            raise CommandError(
                f'League "{league_name}" does not exist. '
                'Create it first via the admin before running this import.'
            )

        existing_active = Season.objects.filter(league=league, status=Season.Status.ACTIVE).first()
        if existing_active and not replace_active:
            raise CommandError(
                f'An active season ("{existing_active.name}") already exists for "{league_name}". '
                'Pass --replace-active to delete it and its weeks/matches/results before importing.'
            )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            with transaction.atomic():
                if existing_active and replace_active:
                    if dry_run:
                        self.stdout.write(self.style.WARNING(
                            f'  [dry] Would delete existing active season "{existing_active.name}" '
                            'and all of its weeks/matches/results.'
                        ))
                    else:
                        self.stdout.write(self.style.WARNING(
                            f'Deleting existing active season "{existing_active.name}" '
                            'and all of its weeks/matches/results...'
                        ))
                        existing_active.delete()

                id_maps = self._import_schedule(conn, league, dry_run)

                if not skip_scores:
                    self._import_scores(conn, id_maps, dry_run)
                else:
                    self.stdout.write(self.style.WARNING('Skipping scores (--skip-scores).'))

                if dry_run:
                    raise _DryRunRollback()

                self.stdout.write(self.style.NOTICE(
                    f"Imported {len(id_maps['match'])} match(es) across {len(id_maps['week'])} week(s)."
                ))
        except _DryRunRollback:
            self.stdout.write(self.style.SUCCESS('Dry run complete — no changes committed.'))
        finally:
            conn.close()

        if not dry_run:
            self.stdout.write(self.style.SUCCESS('Import complete.'))

    # -------------------------------------------------------------------------
    # Schedule import: venues → teams → players → season → weeks → matches
    # -------------------------------------------------------------------------

    def _import_schedule(self, conn, league, dry_run):
        id_maps = {k: {} for k in ('venue', 'team', 'player', 'season', 'week', 'match')}

        self._import_venues(conn, league, id_maps, dry_run)
        self._import_teams(conn, league, id_maps, dry_run)
        self._import_seeds(conn, id_maps, dry_run)
        self._import_players(conn, league, id_maps, dry_run)
        self._import_seasons(conn, league, id_maps, dry_run)
        self._import_weeks(conn, id_maps, dry_run)
        self._import_matches(conn, id_maps, dry_run)

        return id_maps

    # -- Venues ------------------------------------------------------------

    def _import_venues(self, conn, league, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM runner_bar ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Venues: {len(rows)} found in legacy DB'))

        for row in rows:
            name = row['name']
            if dry_run:
                self.stdout.write(f'  [dry] Would create/update venue: {name}')
                continue

            venue, created = Venue.objects.get_or_create(
                league=league,
                name=name,
                defaults={
                    'phone': row['phone'] or '',
                    'address': row['address'] or '',
                    'map_coords': row['map_coords'] or '',
                    # number_of_tables means dartboards here and the legacy
                    # darts schema leaves it NULL for some venues (it's
                    # PositiveIntegerField(>=1) on our side), so fall back to 1.
                    'number_of_tables': row['number_tables'] or 1,
                    'max_home_teams': row['max_home_teams'],
                    'min_home_teams': row['min_home_teams'],
                },
            )
            if not created:
                self.stdout.write(self.style.WARNING(f'  Venue "{name}" already exists — skipped'))
            else:
                self.stdout.write(f'  Created venue: {name}')

            id_maps['venue'][row['id']] = venue.id

    # -- Teams ---------------------------------------------------------------

    def _import_teams(self, conn, league, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM runner_team ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Teams: {len(rows)} found in legacy DB'))

        for row in rows:
            name = row['name']
            old_bar_id = row['bar_id']
            new_venue_id = id_maps['venue'].get(old_bar_id)

            if new_venue_id is None and not dry_run:
                self.stdout.write(self.style.WARNING(
                    f'  Team "{name}" references unknown bar id {old_bar_id} — skipped'
                ))
                continue

            if dry_run:
                self.stdout.write(f'  [dry] Would create/update team: {name}')
                continue

            venue = Venue.objects.get(id=new_venue_id)
            team, created = Team.objects.get_or_create(
                league=league,
                name=name,
                defaults={'venue': venue},
            )
            if not created:
                self.stdout.write(self.style.WARNING(f'  Team "{name}" already exists — skipped'))
            else:
                self.stdout.write(f'  Created team: {name}')

            id_maps['team'][row['id']] = team.id

    # -- Seeds -----------------------------------------------------------------

    def _import_seeds(self, conn, id_maps, dry_run):
        rows = conn.execute(
            'SELECT * FROM runner_seed WHERE team_id IS NOT NULL ORDER BY number'
        ).fetchall()
        self.stdout.write(self.style.NOTICE(f'Seeds: {len(rows)} found in legacy DB'))

        for row in rows:
            old_team_id = row['team_id']
            seed_number = row['number']
            new_team_id = id_maps['team'].get(old_team_id)

            if new_team_id is None:
                continue

            if dry_run:
                self.stdout.write(f'  [dry] Would set seed {seed_number} on team id {old_team_id}')
                continue

            Team.objects.filter(id=new_team_id).update(seed=seed_number)
            self.stdout.write(f'  Set seed {seed_number} on team id {new_team_id}')

    # -- Players ---------------------------------------------------------------

    def _import_players(self, conn, league, id_maps, dry_run):
        # The legacy darts schema's player table has no phone or gender
        # columns, unlike the league_* (pool) schema, so those fields fall
        # back to their model defaults (phone='', male=True) on import.
        rows = conn.execute('SELECT * FROM runner_player ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Players: {len(rows)} found in legacy DB'))

        for row in rows:
            name = row['name']
            old_team_id = row['team_id']
            new_team_id = id_maps['team'].get(old_team_id) if old_team_id else None

            if dry_run:
                self.stdout.write(f'  [dry] Would create/update player: {name}')
                continue

            defaults = {}
            if new_team_id:
                defaults['team'] = Team.objects.get(id=new_team_id)

            player, created = Player.objects.get_or_create(
                league=league,
                name=name,
                defaults=defaults,
            )
            if not created:
                self.stdout.write(self.style.WARNING(f'  Player "{name}" already exists — skipped'))
            else:
                self.stdout.write(f'  Created player: {name}')

            id_maps['player'][row['id']] = player.id

    # -- Season ------------------------------------------------------------

    def _import_seasons(self, conn, league, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM runner_schedule ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Seasons: {len(rows)} found in legacy DB'))

        active_exists = Season.objects.filter(
            league=league, status=Season.Status.ACTIVE
        ).exists()

        for i, row in enumerate(rows):
            name = row['name']

            if i == 0 and not active_exists:
                status = Season.Status.ACTIVE
            else:
                status = Season.Status.WORKING

            if dry_run:
                self.stdout.write(f'  [dry] Would create season: "{name}" ({status})')
                continue

            season, created = Season.objects.get_or_create(
                league=league,
                name=name,
                defaults={'status': status},
            )
            if not created:
                self.stdout.write(self.style.WARNING(f'  Season "{name}" already exists — skipped'))
            else:
                self.stdout.write(f'  Created season: "{name}" ({status})')

            id_maps['season'][row['id']] = season.id

    # -- Weeks -----------------------------------------------------------------

    def _import_weeks(self, conn, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM runner_week ORDER BY schedule_id, number').fetchall()
        self.stdout.write(self.style.NOTICE(f'Weeks: {len(rows)} found in legacy DB'))

        for row in rows:
            old_schedule_id = row['schedule_id']
            new_season_id = id_maps['season'].get(old_schedule_id)

            if new_season_id is None:
                self.stdout.write(self.style.WARNING(
                    f'  Week id {row["id"]} references unknown schedule id {old_schedule_id} — skipped'
                ))
                continue

            if dry_run:
                self.stdout.write(
                    f'  [dry] Would create week {row["number"]} dated {row["date"]}'
                )
                continue

            season = Season.objects.get(id=new_season_id)
            week, created = Week.objects.get_or_create(
                season=season,
                number=row['number'],
                defaults={
                    'date': row['date'],
                    'notes': row['notes'] or '',
                },
            )
            if not created:
                self.stdout.write(self.style.WARNING(
                    f'  Week {row["number"]} in season "{season.name}" already exists — skipped'
                ))
            else:
                self.stdout.write(f'  Created week {row["number"]} ({row["date"]})')

            id_maps['week'][row['id']] = week.id

    # -- Matches ---------------------------------------------------------------

    def _import_matches(self, conn, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM runner_match ORDER BY week_id, id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Matches: {len(rows)} found in legacy DB'))

        sort_counters = {}  # week_id → running sort_order

        for row in rows:
            old_week_id = row['week_id']
            old_home_id = row['home_team_id']
            old_away_id = row['away_team_id']

            new_week_id = id_maps['week'].get(old_week_id)
            new_home_id = id_maps['team'].get(old_home_id)
            new_away_id = id_maps['team'].get(old_away_id)

            missing = []
            if new_week_id is None:
                missing.append(f'week id {old_week_id}')
            if new_home_id is None:
                missing.append(f'home_team id {old_home_id}')
            if new_away_id is None:
                missing.append(f'away_team id {old_away_id}')

            if missing:
                self.stdout.write(self.style.WARNING(
                    f'  Match id {row["id"]} skipped — could not resolve: {", ".join(missing)}'
                ))
                continue

            sort_counters[new_week_id] = sort_counters.get(new_week_id, 0) + 1
            sort_order = sort_counters[new_week_id]

            if dry_run:
                self.stdout.write(
                    f'  [dry] Would create match: team {old_home_id} vs {old_away_id} '
                    f'(week {old_week_id})'
                )
                continue

            week = Week.objects.get(id=new_week_id)
            home_team = Team.objects.get(id=new_home_id)
            away_team = Team.objects.get(id=new_away_id)

            match, created = Match.objects.get_or_create(
                week=week,
                home_team=home_team,
                away_team=away_team,
                defaults={
                    'location': row['venue'] or home_team.venue.name,
                    'sort_order': sort_order,
                },
            )
            if not created:
                self.stdout.write(self.style.WARNING(
                    f'  Match {home_team.name} vs {away_team.name} already exists — skipped'
                ))
            else:
                self.stdout.write(f'  Created match: {home_team.name} vs {away_team.name}')

            id_maps['match'][row['id']] = match.id

    # -- Scores ------------------------------------------------------------

    def _import_scores(self, conn, id_maps, dry_run):
        rows = conn.execute(
            'SELECT * FROM runner_matchworkingscore WHERE bye = 0 AND match_id IS NOT NULL'
        ).fetchall()
        self.stdout.write(self.style.NOTICE(f'Match scores: {len(rows)} found in legacy DB'))

        for row in rows:
            old_match_id = row['match_id']
            new_match_id = id_maps['match'].get(old_match_id)

            if new_match_id is None:
                self.stdout.write(self.style.WARNING(
                    f'  Score for old match id {old_match_id} skipped — match not imported'
                ))
                continue

            home_score = row['home_team_games_won']
            away_score = row['away_team_games_won']

            if dry_run:
                self.stdout.write(
                    f'  [dry] Would create MatchResult for match id {old_match_id}: '
                    f'{home_score}-{away_score}'
                )
                continue

            match = Match.objects.get(id=new_match_id)
            result, created = MatchResult.objects.get_or_create(
                match=match,
                defaults={
                    'home_team_score': home_score,
                    'away_team_score': away_score,
                },
            )
            if not created:
                self.stdout.write(self.style.WARNING(
                    f'  MatchResult for {match} already exists — skipped'
                ))
            else:
                self.stdout.write(f'  Created MatchResult: {home_score}-{away_score} for {match}')

            self._import_player_stats_for_match(row, result, match, id_maps)

    def _import_player_stats_for_match(self, row, match_result, match, id_maps):
        team_size = match.week.season.league.team_size
        slots = [
            ('home_player_1', match.home_team),
            ('home_player_2', match.home_team),
            ('away_player_1', match.away_team),
            ('away_player_2', match.away_team),
        ]

        for prefix, represented_team in slots:
            old_player_id = row[f'{prefix}_id']
            if old_player_id is None:
                continue

            new_player_id = id_maps['player'].get(old_player_id)
            if new_player_id is None:
                self.stdout.write(self.style.WARNING(
                    f'  Player stats for old player id {old_player_id} skipped — player not imported'
                ))
                continue

            PlayerMatchResult.objects.get_or_create(
                match_result=match_result,
                player_id=new_player_id,
                defaults={
                    'represented_team': represented_team,
                    # Per-player wins/losses aren't tracked for darts; losses
                    # is set to satisfy PlayerMatchResult.clean()'s pool-
                    # oriented "losses == team_size - wins" invariant.
                    'losses': team_size,
                    'hat_tricks': row[f'{prefix}_hat_trick'],
                    'three_in_a_beds': row[f'{prefix}_three_in_a_bed'],
                    'white_horses': row[f'{prefix}_white_horse'],
                    'three_in_the_blacks': row[f'{prefix}_three_in_the_black'],
                },
            )


# ---------------------------------------------------------------------------
# Internal sentinel for rolling back a dry run inside a transaction
# ---------------------------------------------------------------------------

class _DryRunRollback(Exception):
    pass
