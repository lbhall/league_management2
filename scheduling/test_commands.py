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
