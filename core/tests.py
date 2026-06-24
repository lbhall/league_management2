from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase

from core.models import League, Player, Team, Venue
from core.views import (
    build_team_standings,
    build_week_schedule_with_byes,
    get_active_league,
    get_active_season,
    get_one_pocket_race_label,
)
from scheduling.models import Season, Week


def make_league(results_type=League.ResultsType.ONE_POCKET, **kwargs):
    defaults = {
        'name': 'Test League',
        'team_size': 1,
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


def make_team(league, venue, name, team_rank=None):
    return Team.objects.create(
        league=league,
        venue=venue,
        name=name,
        team_rank=team_rank,
    )


class LeagueModelTests(TestCase):
    def test_str_returns_name(self):
        league = make_league(name='Bogies East One Pocket League')
        self.assertEqual(str(league), 'Bogies East One Pocket League')


class VenueModelTests(TestCase):
    def test_clean_rejects_max_below_min(self):
        league = make_league()
        venue = Venue(
            league=league,
            name='Bad Venue',
            phone='555-1234',
            address='123 Main St',
            number_of_tables=2,
            max_home_teams=1,
            min_home_teams=2,
        )
        with self.assertRaises(ValidationError):
            venue.clean()

    def test_clean_allows_equal_max_and_min(self):
        league = make_league()
        venue = Venue(
            league=league,
            name='Ok Venue',
            phone='555-1234',
            address='123 Main St',
            number_of_tables=2,
            max_home_teams=2,
            min_home_teams=2,
        )
        venue.clean()  # should not raise


class TeamModelTests(TestCase):
    def test_team_rank_rejected_outside_one_pocket_league(self):
        league = make_league(results_type=League.ResultsType.EIGHT_BALL)
        venue = make_venue(league)
        team = Team(league=league, venue=venue, name='Some Team', team_rank=3)
        with self.assertRaises(ValidationError):
            team.clean()

    def test_team_rank_allowed_in_one_pocket_league(self):
        league = make_league(results_type=League.ResultsType.ONE_POCKET)
        venue = make_venue(league)
        team = Team(league=league, venue=venue, name='Some Team', team_rank=3)
        team.clean()  # should not raise

    def test_save_creates_matching_player_and_captain_for_one_pocket(self):
        league = make_league(results_type=League.ResultsType.ONE_POCKET)
        venue = make_venue(league)
        team = make_team(league, venue, 'Marcus')

        player = Player.objects.get(league=league, name='Marcus')
        team.refresh_from_db()
        self.assertEqual(team.captain_id, player.id)
        self.assertEqual(player.team_id, team.id)

    def test_unique_team_name_per_league_enforced(self):
        league = make_league()
        venue = make_venue(league)
        make_team(league, venue, 'Duplicate')
        with self.assertRaises(Exception):
            Team.objects.create(league=league, venue=venue, name='Duplicate')


class PlayerModelTests(TestCase):
    def test_clean_rejects_team_from_other_league(self):
        league_a = make_league(name='League A')
        league_b = make_league(name='League B')
        venue_b = make_venue(league_b, name='Venue B')
        other_team = make_team(league_b, venue_b, 'Other Team')

        player = Player(league=league_a, name='Stray Player', team=other_team)
        with self.assertRaises(ValidationError):
            player.clean()


class OnePocketRaceLabelTests(TestCase):
    """Covers the home/away ordering fix: the race numbers must follow whichever
    team is passed first, not always "weaker player's number first"."""

    def setUp(self):
        self.league = make_league(results_type=League.ResultsType.ONE_POCKET)
        self.venue = make_venue(self.league)

    def test_equal_ranks_race_to_same_number(self):
        team_a = make_team(self.league, self.venue, 'A', team_rank=3)
        team_b = make_team(self.league, self.venue, 'B', team_rank=3)
        self.assertEqual(get_one_pocket_race_label(team_a, team_b), '8/8')

    def test_label_orders_by_argument_position_not_rank(self):
        weaker = make_team(self.league, self.venue, 'Weaker', team_rank=2)
        stronger = make_team(self.league, self.venue, 'Stronger', team_rank=4)

        # weaker (lower rank number) needs fewer games; stronger needs more.
        self.assertEqual(get_one_pocket_race_label(weaker, stronger), '6/10')
        # Swapping the argument order should swap which number comes first.
        self.assertEqual(get_one_pocket_race_label(stronger, weaker), '10/6')

    def test_missing_rank_returns_empty_string(self):
        ranked = make_team(self.league, self.venue, 'Ranked', team_rank=2)
        unranked = make_team(self.league, self.venue, 'Unranked', team_rank=None)
        self.assertEqual(get_one_pocket_race_label(ranked, unranked), '')


class BuildTeamStandingsTests(TestCase):
    def test_no_active_season_sorts_alphabetically_with_zero_records(self):
        league = make_league()
        venue = make_venue(league)
        make_team(league, venue, 'Zed')
        make_team(league, venue, 'Alpha')

        standings = build_team_standings(league, active_season=None)

        self.assertEqual([s['team'] for s in standings], ['Alpha', 'Zed'])
        self.assertTrue(all(s['matches_won'] == 0 for s in standings))


class BuildWeekScheduleWithByesTests(TestCase):
    def test_unscheduled_team_appears_as_bye(self):
        league = make_league()
        venue = make_venue(league)
        scheduled_home = make_team(league, venue, 'Home Team')
        scheduled_away = make_team(league, venue, 'Away Team')
        bye_team = make_team(league, venue, 'Sitting Out')

        season = Season.objects.create(league=league, name='Season 1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date='2026-01-01', number=1)
        week.matches.create(home_team=scheduled_home, away_team=scheduled_away)

        entries = build_week_schedule_with_byes(league, week)

        bye_entries = [e for e in entries if e['is_bye']]
        self.assertEqual(len(bye_entries), 1)
        self.assertEqual(bye_entries[0]['home_team'], bye_team)
        self.assertEqual(bye_entries[0]['away_team'], 'BYE')

        match_entries = [e for e in entries if not e['is_bye']]
        self.assertEqual(len(match_entries), 1)
        self.assertEqual(match_entries[0]['home_team'], scheduled_home)
        self.assertEqual(match_entries[0]['away_team'], scheduled_away)


class ActiveLeagueSeasonHelperTests(TestCase):
    def test_get_active_league_falls_back_to_first_league_when_unset(self):
        make_league(name='Only League')
        request = RequestFactory().get('/')
        request.session = {}

        with self.settings(FRONTEND_LEAGUE_ID=None):
            league = get_active_league(request)

        self.assertEqual(league.name, 'Only League')

    def test_get_active_season_returns_only_active_season(self):
        league = make_league()
        Season.objects.create(league=league, name='Working', status=Season.Status.WORKING)
        active_season = Season.objects.create(league=league, name='Active', status=Season.Status.ACTIVE)

        self.assertEqual(get_active_season(league), active_season)

    def test_get_active_season_returns_none_without_league(self):
        self.assertIsNone(get_active_season(None))
