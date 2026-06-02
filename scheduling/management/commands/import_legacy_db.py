"""
Management command to import data from the legacy emcfunleague SQLite database.

Imports:
  - Venues (league_bar)
  - Teams (league_team) + seeds (league_seed)
  - Players (league_player)
  - Live seasons/weeks/matches (league_schedule, league_week, league_match)
  - Match results (league_matchworkingscore → MatchResult)
  - Player scores (league_playerscore → PlayerMatchResult)
  - Archived seasons/teams/players (league_archiveseason, league_teamarchive, league_playerarchive)

Usage:
  python manage.py import_legacy_db \\
      --db /path/to/old.db.sqlite3 \\
      --league "EMC Fun Pool League" \\
      [--dry-run] \\
      [--skip-schedule] \\
      [--skip-scores] \\
      [--skip-archives]
"""

import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import League, Venue, Team, Player
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import ArchivedPlayer, ArchivedSeason, ArchivedTeam, Match, Season, Week


class Command(BaseCommand):
    help = 'Import venues, teams, players, schedules, and archives from a legacy SQLite database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--db',
            required=True,
            help='Path to the legacy SQLite database file.',
        )
        parser.add_argument(
            '--league',
            required=True,
            help='Name of the target league to import data into.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate the import without writing anything to the database.',
        )
        parser.add_argument(
            '--skip-schedule',
            action='store_true',
            help='Skip importing venues, teams, players, and live schedule data.',
        )
        parser.add_argument(
            '--skip-scores',
            action='store_true',
            help='Skip importing match and player score results.',
        )
        parser.add_argument(
            '--skip-archives',
            action='store_true',
            help='Skip importing archived season/team/player records.',
        )

    # -------------------------------------------------------------------------
    # Entry point
    # -------------------------------------------------------------------------

    def handle(self, *args, **options):
        db_path = Path(options['db']).expanduser()
        league_name = options['league']
        dry_run = options['dry_run']

        self.stdout.write(self.style.NOTICE(f'Legacy DB : {db_path}'))
        self.stdout.write(self.style.NOTICE(f'League    : {league_name}'))
        self.stdout.write(self.style.NOTICE(f'Dry run   : {"yes" if dry_run else "no"}'))

        if not db_path.exists():
            raise CommandError(f'Database file not found: {db_path}')

        try:
            league = League.objects.get(name=league_name)
        except League.DoesNotExist:
            raise CommandError(
                f'League "{league_name}" does not exist. '
                'Create it first via the admin before running this import.'
            )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            with transaction.atomic():
                if not options['skip_schedule']:
                    id_maps = self._import_schedule(conn, league, dry_run)
                    if not options['skip_scores']:
                        self._import_scores(conn, id_maps, dry_run)
                elif not options['skip_scores']:
                    self.stdout.write(self.style.WARNING(
                        'Skipping scores because --skip-schedule was set '
                        '(scores depend on imported match IDs).'
                    ))

                if not options['skip_archives']:
                    self._import_archives(conn, league, dry_run)

                if dry_run:
                    raise _DryRunRollback()

        except _DryRunRollback:
            self.stdout.write(self.style.SUCCESS('Dry run complete — no changes committed.'))
        finally:
            conn.close()

        if not dry_run:
            self.stdout.write(self.style.SUCCESS('Import complete.'))

    # -------------------------------------------------------------------------
    # Schedule import: venues → teams → players → seasons → weeks → matches
    # -------------------------------------------------------------------------

    def _import_schedule(self, conn, league, dry_run):
        """
        Returns id_maps dict:
          {
            'venue':  {old_bar_id: new_venue_id},
            'team':   {old_team_id: new_team_id},
            'player': {old_player_id: new_player_id},
            'season': {old_schedule_id: new_season_id},
            'week':   {old_week_id: new_week_id},
            'match':  {old_match_id: new_match_id},
          }
        """
        id_maps = {k: {} for k in ('venue', 'team', 'player', 'season', 'week', 'match')}

        self._import_venues(conn, league, id_maps, dry_run)
        self._import_teams(conn, league, id_maps, dry_run)
        self._import_seeds(conn, id_maps, dry_run)
        self._import_players(conn, league, id_maps, dry_run)
        self._import_seasons(conn, league, id_maps, dry_run)
        self._import_weeks(conn, id_maps, dry_run)
        self._import_matches(conn, id_maps, dry_run)

        return id_maps

    # -- Venues ----------------------------------------------------------------

    def _import_venues(self, conn, league, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM league_bar ORDER BY id').fetchall()
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
                    'number_of_tables': row['number_tables'],
                    'max_home_teams': row['max_home_teams'],
                    'min_home_teams': row['min_home_teams'],
                },
            )
            if not created:
                self.stdout.write(self.style.WARNING(f'  Venue "{name}" already exists — skipped'))
            else:
                self.stdout.write(f'  Created venue: {name}')

            id_maps['venue'][row['id']] = venue.id

    # -- Teams -----------------------------------------------------------------

    def _import_teams(self, conn, league, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM league_team ORDER BY id').fetchall()
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
            'SELECT * FROM league_seed WHERE team_id IS NOT NULL ORDER BY number'
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
        rows = conn.execute('SELECT * FROM league_player ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Players: {len(rows)} found in legacy DB'))

        for row in rows:
            name = row['name']
            old_team_id = row['team_id']
            new_team_id = id_maps['team'].get(old_team_id) if old_team_id else None

            if dry_run:
                self.stdout.write(f'  [dry] Would create/update player: {name}')
                continue

            defaults = {
                'phone': row['phone'] or '',
                'male': bool(row['male']),
            }
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

    # -- Seasons ---------------------------------------------------------------

    def _import_seasons(self, conn, league, id_maps, dry_run):
        rows = conn.execute('SELECT * FROM league_schedule ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Seasons: {len(rows)} found in legacy DB'))

        active_exists = Season.objects.filter(
            league=league, status=Season.Status.ACTIVE
        ).exists()

        for i, row in enumerate(rows):
            name = row['name']

            # Assign the first (or only) season as active if none exists yet
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
        rows = conn.execute('SELECT * FROM league_week ORDER BY schedule_id, number').fetchall()
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
        rows = conn.execute('SELECT * FROM league_match ORDER BY week_id, id').fetchall()
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

    # -------------------------------------------------------------------------
    # Scores: match results + player scores
    # -------------------------------------------------------------------------

    def _import_scores(self, conn, id_maps, dry_run):
        self._import_match_results(conn, id_maps, dry_run)
        self._import_player_scores(conn, id_maps, dry_run)

    def _import_match_results(self, conn, id_maps, dry_run):
        rows = conn.execute(
            'SELECT * FROM league_matchworkingscore WHERE bye = 0 AND match_id IS NOT NULL'
        ).fetchall()
        self.stdout.write(self.style.NOTICE(f'Match results: {len(rows)} found in legacy DB'))

        for row in rows:
            old_match_id = row['match_id']
            new_match_id = id_maps['match'].get(old_match_id)

            if new_match_id is None:
                self.stdout.write(self.style.WARNING(
                    f'  MatchResult for old match id {old_match_id} skipped — match not imported'
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

    def _import_player_scores(self, conn, id_maps, dry_run):
        """
        league_playerscore rows are linked to a date + player + team.
        We resolve the match by finding the Week with that date in the imported seasons,
        then the Match where that team played in that week.
        """
        rows = conn.execute('SELECT * FROM league_playerscore ORDER BY id').fetchall()
        self.stdout.write(self.style.NOTICE(f'Player scores: {len(rows)} found in legacy DB'))

        # Build reverse lookup: new_week.date → new_week_id for fast resolution
        imported_week_ids = set(id_maps['week'].values())
        date_to_week_id = {}
        for week in Week.objects.filter(id__in=imported_week_ids):
            date_str = week.date.isoformat()
            date_to_week_id.setdefault(date_str, []).append(week.id)

        skipped = 0
        created_count = 0

        for row in rows:
            old_player_id = row['player_id']
            old_team_id = row['team_id']
            score_date = row['date']

            new_player_id = id_maps['player'].get(old_player_id)
            new_team_id = id_maps['team'].get(old_team_id)

            if new_player_id is None or new_team_id is None:
                skipped += 1
                continue

            # Find the week(s) matching this date
            week_ids_for_date = date_to_week_id.get(score_date, [])
            if not week_ids_for_date:
                skipped += 1
                continue

            # Find the match in those weeks where this team participated
            match = Match.objects.filter(
                week_id__in=week_ids_for_date,
            ).filter(
                home_team_id=new_team_id
            ).first() or Match.objects.filter(
                week_id__in=week_ids_for_date,
            ).filter(
                away_team_id=new_team_id
            ).first()

            if match is None:
                skipped += 1
                continue

            # Ensure the MatchResult exists
            match_result = MatchResult.objects.filter(match=match).first()
            if match_result is None:
                skipped += 1
                continue

            player = Player.objects.get(id=new_player_id)
            team = Team.objects.get(id=new_team_id)

            if dry_run:
                self.stdout.write(
                    f'  [dry] Would create PlayerMatchResult for player "{player.name}" '
                    f'in match {match}'
                )
                created_count += 1
                continue

            _, created = PlayerMatchResult.objects.get_or_create(
                match_result=match_result,
                player=player,
                defaults={
                    'represented_team': team,
                    'wins': row['games_won'],
                    'losses': row['games_lost'],
                    'runouts': row['run_outs'],
                    'eight_on_the_breaks': row['eight_on_break'],
                    'won_all_games': row['five_and_o'] > 0,
                },
            )
            if created:
                created_count += 1
            else:
                skipped += 1

        self.stdout.write(
            f'  Player scores: {created_count} imported, {skipped} skipped'
        )

    # -------------------------------------------------------------------------
    # Archives
    # -------------------------------------------------------------------------

    def _import_archives(self, conn, league, dry_run):
        archive_rows = conn.execute(
            'SELECT * FROM league_archiveseason ORDER BY id'
        ).fetchall()
        self.stdout.write(self.style.NOTICE(
            f'Archived seasons: {len(archive_rows)} found in legacy DB'
        ))

        for archive_row in archive_rows:
            old_season_id = archive_row['id']
            season_name = archive_row['period']

            if dry_run:
                self.stdout.write(f'  [dry] Would create ArchivedSeason: "{season_name}"')
            else:
                arch_season, created = ArchivedSeason.objects.get_or_create(
                    league=league,
                    name=season_name,
                )
                if not created:
                    self.stdout.write(self.style.WARNING(
                        f'  ArchivedSeason "{season_name}" already exists — skipped'
                    ))
                    arch_season_id = arch_season.id
                else:
                    self.stdout.write(f'  Created ArchivedSeason: "{season_name}"')
                    arch_season_id = arch_season.id

            # Archived teams for this season
            team_rows = conn.execute(
                'SELECT * FROM league_teamarchive WHERE season_id = ? ORDER BY rank',
                (old_season_id,),
            ).fetchall()

            for team_row in team_rows:
                if dry_run:
                    self.stdout.write(
                        f'    [dry] Would create ArchivedTeam: "{team_row["name"]}"'
                    )
                    continue

                ArchivedTeam.objects.get_or_create(
                    archived_season_id=arch_season_id,
                    team_name=team_row['name'],
                    defaults={
                        'matches_won': team_row['matches_won'],
                        'matches_lost': team_row['matches_lost'],
                        'games_won': team_row['games_won'],
                        'games_lost': team_row['games_lost'],
                    },
                )

            self.stdout.write(f'    Imported {len(team_rows)} archived teams')

            # Archived players for this season
            player_rows = conn.execute(
                'SELECT * FROM league_playerarchive WHERE season_id = ? ORDER BY rank',
                (old_season_id,),
            ).fetchall()

            for player_row in player_rows:
                if dry_run:
                    self.stdout.write(
                        f'    [dry] Would create ArchivedPlayer: "{player_row["name"]}"'
                    )
                    continue

                ArchivedPlayer.objects.get_or_create(
                    archived_season_id=arch_season_id,
                    player_name=player_row['name'],
                    defaults={
                        'team_name': player_row['team'] or '',
                        'games_won': player_row['games_won'],
                        'games_lost': player_row['games_lost'],
                        'run_outs': player_row['rack_and_runs'],
                        'eight_on_the_breaks': player_row['eight_on_break'],
                        'sweeps': player_row['five_and_o'],
                    },
                )

            self.stdout.write(f'    Imported {len(player_rows)} archived players')


# ---------------------------------------------------------------------------
# Internal sentinel for rolling back a dry run inside a transaction
# ---------------------------------------------------------------------------

class _DryRunRollback(Exception):
    pass
