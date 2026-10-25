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
