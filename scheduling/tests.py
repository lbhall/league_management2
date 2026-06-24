from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import League, Team, Venue
from scheduling.models import Match, Season, Week


def make_league(results_type=League.ResultsType.EIGHT_BALL, **kwargs):
    defaults = {
        'name': 'Test League',
        'team_size': 3,
        'results_type': results_type,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Test Venue'):
    return Venue.objects.create(
        league=league,
        name=name,
        phone='555-1234',
        address='123 Main St',
        number_of_tables=2,
        max_home_teams=4,
        min_home_teams=1,
    )


def make_team(league, venue, name):
    return Team.objects.create(league=league, venue=venue, name=name)


class SeasonModelTests(TestCase):
    def test_only_one_working_season_per_league(self):
        league = make_league()
        Season.objects.create(league=league, name='Season 1', status=Season.Status.WORKING)

        duplicate = Season(league=league, name='Season 2', status=Season.Status.WORKING)
        with self.assertRaises(ValidationError):
            duplicate.clean()

    def test_only_one_active_season_per_league_db_constraint(self):
        league = make_league()
        Season.objects.create(league=league, name='Season 1', status=Season.Status.ACTIVE)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Season.objects.create(league=league, name='Season 2', status=Season.Status.ACTIVE)

    def test_different_leagues_can_each_have_a_working_season(self):
        league_a = make_league(name='League A')
        league_b = make_league(name='League B')
        Season.objects.create(league=league_a, name='Season 1', status=Season.Status.WORKING)
        # Should not raise.
        Season.objects.create(league=league_b, name='Season 1', status=Season.Status.WORKING)


class WeekModelTests(TestCase):
    def test_clean_requires_notes_when_number_missing(self):
        league = make_league()
        season = Season.objects.create(league=league, name='Season 1', status=Season.Status.ACTIVE)
        week = Week(season=season, date='2026-01-01', number=None, notes='')
        with self.assertRaises(ValidationError):
            week.clean()

    def test_clean_allows_missing_number_with_notes(self):
        league = make_league()
        season = Season.objects.create(league=league, name='Season 1', status=Season.Status.ACTIVE)
        week = Week(season=season, date='2026-01-01', number=None, notes='Holiday break')
        week.clean()  # should not raise
        self.assertTrue(week.is_holiday())

    def test_is_holiday_false_when_number_present(self):
        league = make_league()
        season = Season.objects.create(league=league, name='Season 1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date='2026-01-01', number=1)
        self.assertFalse(week.is_holiday())


class MatchModelTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.venue = make_venue(self.league)
        self.home_team = make_team(self.league, self.venue, 'Home Team')
        self.away_team = make_team(self.league, self.venue, 'Away Team')
        self.season = Season.objects.create(league=self.league, name='Season 1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date='2026-01-01', number=1)

    def test_clean_rejects_home_equal_to_away(self):
        match = Match(week=self.week, home_team=self.home_team, away_team=self.home_team)
        with self.assertRaises(ValidationError):
            match.clean()

    def test_clean_rejects_team_from_different_league(self):
        other_league = make_league(name='Other League')
        other_venue = make_venue(other_league, name='Other Venue')
        foreign_team = make_team(other_league, other_venue, 'Foreign Team')

        match = Match(week=self.week, home_team=foreign_team, away_team=self.away_team)
        with self.assertRaises(ValidationError):
            match.clean()

    def test_save_auto_fills_location_from_home_venue(self):
        match = Match.objects.create(week=self.week, home_team=self.home_team, away_team=self.away_team)
        self.assertEqual(match.location, self.venue.name)

    def test_save_preserves_explicit_location(self):
        match = Match.objects.create(
            week=self.week,
            home_team=self.home_team,
            away_team=self.away_team,
            location='Custom Location',
        )
        self.assertEqual(match.location, 'Custom Location')
