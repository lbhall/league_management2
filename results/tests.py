from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import League, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season, Week


def make_league(results_type=League.ResultsType.EIGHT_BALL, team_size=3, **kwargs):
    defaults = {
        'name': 'Test League',
        'team_size': team_size,
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


def make_match(league, home_team, away_team):
    season = Season.objects.create(league=league, name='Season 1', status=Season.Status.ACTIVE)
    week = Week.objects.create(season=season, date='2026-01-01', number=1)
    return Match.objects.create(week=week, home_team=home_team, away_team=away_team)


class MatchResultModelTests(TestCase):
    def test_clean_rejects_unsupported_results_type(self):
        league = make_league(results_type=League.ResultsType.EIGHT_BALL)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        match = make_match(league, home, away)
        League.objects.filter(pk=league.pk).update(results_type='something_else')
        match.week.season.league.refresh_from_db()

        result = MatchResult(match=match)
        with self.assertRaises(ValidationError):
            result.clean()

    def test_clean_allows_darts(self):
        league = make_league(results_type=League.ResultsType.DARTS)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        match = make_match(league, home, away)

        result = MatchResult(match=match)
        result.clean()  # should not raise

    def test_clean_allows_eight_ball(self):
        league = make_league(results_type=League.ResultsType.EIGHT_BALL)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        match = make_match(league, home, away)

        result = MatchResult(match=match)
        result.clean()  # should not raise

    def test_clean_allows_one_pocket(self):
        league = make_league(results_type=League.ResultsType.ONE_POCKET)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        match = make_match(league, home, away)

        result = MatchResult(match=match)
        result.clean()  # should not raise


class PlayerMatchResultModelTests(TestCase):
    def setUp(self):
        self.league = make_league(results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        self.venue = make_venue(self.league)
        self.home_team = make_team(self.league, self.venue, 'Home')
        self.away_team = make_team(self.league, self.venue, 'Away')
        self.match = make_match(self.league, self.home_team, self.away_team)
        self.match_result = MatchResult.objects.create(match=self.match)
        self.player = Player.objects.create(league=self.league, name='Alice', team=self.home_team)

    def test_save_auto_calculates_losses_and_won_all_games(self):
        pmr = PlayerMatchResult.objects.create(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=3,
        )
        self.assertEqual(pmr.losses, 0)
        self.assertTrue(pmr.won_all_games)

    def test_darts_points_weights_each_stat(self):
        pmr = PlayerMatchResult(
            hat_tricks=2, three_in_a_beds=1, white_horses=1, three_in_the_blacks=1,
        )
        self.assertEqual(pmr.darts_points, 2 * 1 + 1 * 2 + 1 * 3 + 1 * 4)

    def test_darts_points_defaults_to_zero(self):
        pmr = PlayerMatchResult()
        self.assertEqual(pmr.darts_points, 0)

    def test_save_recomputes_losses_for_partial_wins(self):
        pmr = PlayerMatchResult.objects.create(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=1,
        )
        self.assertEqual(pmr.losses, 2)
        self.assertFalse(pmr.won_all_games)

    def test_clean_rejects_represented_team_not_in_match(self):
        other_league = make_league(name='Other League')
        other_venue = make_venue(other_league, name='Other Venue')
        foreign_team = make_team(other_league, other_venue, 'Foreign Team')

        pmr = PlayerMatchResult(
            match_result=self.match_result,
            player=self.player,
            represented_team=foreign_team,
            wins=1,
        )
        with self.assertRaises(ValidationError):
            pmr.clean()

    def test_clean_rejects_player_from_different_league(self):
        other_league = make_league(name='Other League')
        foreign_player = Player.objects.create(league=other_league, name='Bob')

        pmr = PlayerMatchResult(
            match_result=self.match_result,
            player=foreign_player,
            represented_team=self.home_team,
            wins=1,
        )
        with self.assertRaises(ValidationError):
            pmr.clean()

    def test_clean_rejects_player_assigned_to_unrelated_team(self):
        unrelated_team = make_team(self.league, self.venue, 'Unrelated')
        self.player.team = unrelated_team
        self.player.save()

        pmr = PlayerMatchResult(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=1,
        )
        with self.assertRaises(ValidationError):
            pmr.clean()

    def test_clean_rejects_wins_above_team_size(self):
        pmr = PlayerMatchResult(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=4,
        )
        with self.assertRaises(ValidationError):
            pmr.clean()

    def test_clean_rejects_inconsistent_losses(self):
        pmr = PlayerMatchResult(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=1,
            losses=0,
        )
        with self.assertRaises(ValidationError):
            pmr.clean()

    def test_clean_rejects_won_all_games_with_remaining_losses(self):
        pmr = PlayerMatchResult(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=2,
            losses=1,
            won_all_games=True,
        )
        with self.assertRaises(ValidationError):
            pmr.clean()

    def test_unique_player_per_match_result_enforced(self):
        PlayerMatchResult.objects.create(
            match_result=self.match_result,
            player=self.player,
            represented_team=self.home_team,
            wins=1,
        )
        with self.assertRaises(Exception):
            PlayerMatchResult.objects.create(
                match_result=self.match_result,
                player=self.player,
                represented_team=self.home_team,
                wins=2,
            )
