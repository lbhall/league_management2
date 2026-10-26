from datetime import date

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from scheduling.models import Match, Season, Week
from scheduling.test_services import make_league, make_teams, make_venue


class CompressScheduleCommandTests(TestCase):
    def setUp(self):
        self.league = make_league(name='Command League')
        self.venue = make_venue(self.league, max_home_teams=4)
        self.team_a, self.team_b, self.team_c, self.team_d = make_teams(self.league, self.venue, 4)

    def _sparse_working_season(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        week2 = Week.objects.create(season=season, date=date(2026, 1, 12), number=2)
        Match.objects.create(week=week1, home_team=self.team_a, away_team=self.team_b, location=self.venue.name)
        Match.objects.create(week=week2, home_team=self.team_c, away_team=self.team_d, location=self.venue.name)
        return season

    def test_compresses_the_working_season(self):
        season = self._sparse_working_season()
        call_command('compress_schedule', league=self.league.name, seed=1)
        self.assertEqual(season.weeks.count(), 1)
        self.assertEqual(Match.objects.filter(week__season=season).count(), 2)

    def test_errors_without_a_working_season(self):
        with self.assertRaises(CommandError):
            call_command('compress_schedule', league=self.league.name)

    def test_errors_for_unknown_league(self):
        with self.assertRaises(CommandError):
            call_command('compress_schedule', league='Nope League')


class SwapWeeksCommandTests(TestCase):
    def setUp(self):
        self.league = make_league(name='Swap League')
        self.venue = make_venue(self.league, max_home_teams=4)
        self.team_a, self.team_b, self.team_c, self.team_d = make_teams(self.league, self.venue, 4)
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.week2 = Week.objects.create(season=self.season, date=date(2026, 1, 12), number=2)
        self.m1 = Match.objects.create(week=self.week1, home_team=self.team_a, away_team=self.team_b, location=self.venue.name)
        self.m2 = Match.objects.create(week=self.week2, home_team=self.team_c, away_team=self.team_d, location=self.venue.name)

    def test_dry_run_changes_nothing(self):
        call_command('swap_weeks', 1, 2, league=self.league.name)
        self.m1.refresh_from_db()
        self.assertEqual(self.m1.week, self.week1)

    def test_apply_swaps_the_matches_but_not_the_weeks(self):
        call_command('swap_weeks', 1, 2, league=self.league.name, apply=True)
        self.m1.refresh_from_db()
        self.m2.refresh_from_db()
        self.assertEqual(self.m1.week, self.week2)
        self.assertEqual(self.m2.week, self.week1)
        self.week1.refresh_from_db()
        self.assertEqual((self.week1.number, self.week1.date), (1, date(2026, 1, 5)))

    def test_refuses_when_a_result_exists(self):
        from results.models import MatchResult
        MatchResult.objects.create(match=self.m1, home_team_score=3, away_team_score=1)
        with self.assertRaises(CommandError):
            call_command('swap_weeks', 1, 2, league=self.league.name, apply=True)

    def test_errors_for_missing_week(self):
        with self.assertRaises(CommandError):
            call_command('swap_weeks', 1, 9, league=self.league.name)


class RestoreSeasonFromBackupCommandTests(TestCase):
    """Builds a tiny old-schema SQLite backup and restores a season out of it."""

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        self.league = make_league(name='Restore League', results_type='8_ball', team_size=3)
        self.venue = make_venue(self.league, max_home_teams=4)
        self.team_a, self.team_b = make_teams(self.league, self.venue, 2)
        from core.models import Player
        self.alice = Player.objects.create(league=self.league, name='Alice', team=self.team_a, male=False)

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'old.sqlite3')
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE scheduling_season (id INTEGER PRIMARY KEY, name TEXT, status TEXT, league_id INTEGER);
            CREATE TABLE scheduling_week (id INTEGER PRIMARY KEY, date TEXT, number INTEGER, notes TEXT, season_id INTEGER);
            CREATE TABLE scheduling_match (id INTEGER PRIMARY KEY, location TEXT, away_team_id INTEGER, home_team_id INTEGER, week_id INTEGER, sort_order INTEGER);
            CREATE TABLE results_matchresult (id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT, match_id INTEGER, away_team_score INTEGER, home_team_score INTEGER);
            CREATE TABLE results_playermatchresult (id INTEGER PRIMARY KEY, wins INTEGER, losses INTEGER, runouts INTEGER, eight_on_the_breaks INTEGER, won_all_games INTEGER, match_result_id INTEGER, player_id INTEGER, represented_team_id INTEGER);
            CREATE TABLE core_team (id INTEGER PRIMARY KEY, name TEXT, captain_id INTEGER, league_id INTEGER, venue_id INTEGER, seed INTEGER, team_rank INTEGER);
            CREATE TABLE core_player (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, male INTEGER, league_id INTEGER, team_id INTEGER);
            INSERT INTO scheduling_season VALUES (12, 'Early 2026', 'active', 7);
            INSERT INTO scheduling_week VALUES (1, '2026-04-13', 1, '', 12), (2, '2026-04-20', NULL, 'Holiday', 12);
            INSERT INTO core_team VALUES (101, 'Team 1', NULL, 7, 1, NULL, NULL), (102, 'Team 2', NULL, 7, 1, NULL, NULL);
            INSERT INTO core_player VALUES (201, 'Alice', '', 0, 7, 101), (202, 'Bob', '555', 1, 7, 102);
            INSERT INTO scheduling_match VALUES (301, 'Venue', 102, 101, 1, 0);
            INSERT INTO results_matchresult VALUES (401, '2026-04-14 03:10:00', '2026-04-14 03:10:00', 301, 1, 2);
            INSERT INTO results_playermatchresult VALUES (501, 2, 1, 1, 0, 0, 401, 201, 101), (502, 1, 2, 0, 1, 0, 401, 202, 102);
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_creates_nothing(self):
        call_command('restore_season_from_backup', db=self.db_path, league=self.league.name, season_id=12)
        self.assertFalse(Season.objects.filter(league=self.league).exists())

    def test_apply_restores_weeks_matches_results_and_players(self):
        from core.models import Player
        from results.models import MatchResult, PlayerMatchResult
        call_command('restore_season_from_backup', db=self.db_path, league=self.league.name, season_id=12,
                     status='active', apply=True)
        season = Season.objects.get(league=self.league, name='Early 2026')
        self.assertEqual(season.status, Season.Status.ACTIVE)
        self.assertEqual(season.weeks.count(), 2)
        self.assertEqual(season.weeks.get(number__isnull=True).notes, 'Holiday')
        match = Match.objects.get(week__season=season)
        self.assertEqual((match.home_team, match.away_team), (self.team_a, self.team_b))
        result = MatchResult.objects.get(match=match)
        self.assertEqual((result.home_team_score, result.away_team_score), (2, 1))
        self.assertEqual(result.created_at.year, 2026)
        bob = Player.objects.get(league=self.league, name='Bob')
        self.assertEqual(bob.team, self.team_b)
        self.assertEqual(Player.objects.filter(league=self.league, name='Alice').count(), 1)
        scores = {s.player.name: s for s in PlayerMatchResult.objects.filter(match_result=result)}
        self.assertEqual((scores['Alice'].wins, scores['Alice'].runouts), (2, 1))
        self.assertEqual((scores['Bob'].wins, scores['Bob'].eight_on_the_breaks), (1, 1))

    def test_refuses_duplicate_name_or_second_active_season(self):
        Season.objects.create(league=self.league, name='Early 2026', status=Season.Status.WORKING)
        with self.assertRaises(CommandError):
            call_command('restore_season_from_backup', db=self.db_path, league=self.league.name, season_id=12)
        # A different name is fine while no season is active...
        call_command('restore_season_from_backup', db=self.db_path, league=self.league.name, season_id=12,
                     name='Copy', status='active')
        Season.objects.create(league=self.league, name='Current', status=Season.Status.ACTIVE)
        with self.assertRaises(CommandError):
            call_command('restore_season_from_backup', db=self.db_path, league=self.league.name, season_id=12,
                         name='Copy', status='active')

    def test_errors_for_missing_backup_or_season(self):
        with self.assertRaises(CommandError):
            call_command('restore_season_from_backup', db=self.db_path, league=self.league.name, season_id=99)
        with self.assertRaises(CommandError):
            call_command('restore_season_from_backup', db=self.db_path + '.nope', league=self.league.name, season_id=12)


class LinkArchivedPlayersCommandTests(TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from core.models import Player
        from scheduling.models import ArchivedPlayer, ArchivedSeason

        self.league = make_league(name='Link League', results_type='8_ball', team_size=5)
        self.venue = make_venue(self.league)
        self.team, = make_teams(self.league, self.venue, 1)
        self.wayman = Player.objects.create(league=self.league, name='Wayman', team=self.team)
        self.other = Player.objects.create(league=self.league, name='Todd', team=self.team)
        self.old = ArchivedSeason.objects.create(league=self.league, name='2023', results_type='8_ball', team_size=5)
        self.older = ArchivedSeason.objects.create(league=self.league, name='2024', results_type='8_ball', team_size=5)
        self.row1 = ArchivedPlayer.objects.create(archived_season=self.old, player_name='Wayman Biles', games_won=30, games_lost=10)
        self.row2 = ArchivedPlayer.objects.create(archived_season=self.older, player_name='Wayman Biles', games_won=20, games_lost=20)
        self.sub = ArchivedPlayer.objects.create(archived_season=self.older, player_name='Wayman (SUB)', games_won=5, games_lost=5)
        self.unrelated = ArchivedPlayer.objects.create(archived_season=self.old, player_name='Todd Gurka', games_won=1)
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = str(Path(self.tmp.name) / 'links.csv')

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.csv_path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)

    def test_dry_run_links_nothing(self):
        self._write('archived_name,live_name,apply,note\nWayman Biles,Wayman,yes,\n')
        call_command('link_archived_players', league=self.league.name, csv=self.csv_path)
        self.row1.refresh_from_db()
        self.assertIsNone(self.row1.player)

    def test_apply_links_every_archive_row_with_that_name(self):
        self._write('archived_name,live_name,apply,note\nWayman Biles,Wayman,yes,\nWayman (SUB),Wayman,yes,\nTodd Gurka,Todd,no,CHECK\n')
        call_command('link_archived_players', league=self.league.name, csv=self.csv_path, apply=True)
        for row in (self.row1, self.row2, self.sub):
            row.refresh_from_db()
            self.assertEqual(row.player, self.wayman)
        self.unrelated.refresh_from_db()
        self.assertIsNone(self.unrelated.player)  # apply=no is skipped
        rows, totals = __import__('scheduling.services', fromlist=['x']).build_player_season_history(self.wayman)
        self.assertEqual(len(rows), 3)
        self.assertEqual(totals['games_won'], 55)

    def test_refuses_to_cross_the_sub_line(self):
        from core.models import Player
        Player.objects.create(league=self.league, name='Wayman (SUB)', team=None)
        self._write('archived_name,live_name,apply,note\nWayman Biles,Wayman,yes,\nWayman (SUB),Wayman,yes,\n')
        with self.assertRaises(CommandError):
            call_command('link_archived_players', league=self.league.name, csv=self.csv_path, apply=True)
        self.row1.refresh_from_db()
        self.assertIsNone(self.row1.player)  # nothing applied when any row is refused
        self._write('archived_name,live_name,apply,note\nWayman (SUB),Wayman (SUB),yes,\n')
        call_command('link_archived_players', league=self.league.name, csv=self.csv_path, apply=True)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.player.name, 'Wayman (SUB)')

    def test_errors_for_unknown_or_ambiguous_live_name(self):
        from core.models import Player
        self._write('archived_name,live_name,apply,note\nWayman Biles,Nobody,yes,\n')
        with self.assertRaises(CommandError):
            call_command('link_archived_players', league=self.league.name, csv=self.csv_path, apply=True)
        Player.objects.create(league=self.league, name='Wayman', team=self.team)
        self._write('archived_name,live_name,apply,note\nWayman Biles,Wayman,yes,\n')
        with self.assertRaises(CommandError):
            call_command('link_archived_players', league=self.league.name, csv=self.csv_path, apply=True)
        self.row1.refresh_from_db()
        self.assertIsNone(self.row1.player)
