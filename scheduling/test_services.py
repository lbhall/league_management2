from datetime import date

from django.test import TestCase

from core.models import League, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling import services
from scheduling.models import (
    ArchivedSeason,
    Holiday,
    Match,
    Season,
    Week,
)


def make_league(results_type=League.ResultsType.ONE_POCKET, team_size=1, day_of_week=League.DayOfWeek.MONDAY, **kwargs):
    defaults = {
        'name': 'Services League',
        'team_size': team_size,
        'results_type': results_type,
        'day_of_week': day_of_week,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Venue', max_home_teams=4, min_home_teams=1):
    return Venue.objects.create(
        league=league,
        name=name,
        phone='555-0000',
        address='123 Main St',
        number_of_tables=2,
        max_home_teams=max_home_teams,
        min_home_teams=min_home_teams,
    )


def make_team(league, venue, name):
    return Team.objects.create(league=league, venue=venue, name=name)


def make_teams(league, venue, count, prefix='Team'):
    return [make_team(league, venue, f'{prefix} {i}') for i in range(1, count + 1)]


class GetNextStartDatesTests(TestCase):
    def test_returns_requested_count_on_correct_weekday(self):
        league = make_league(day_of_week=League.DayOfWeek.WEDNESDAY)
        # 2026-01-05 is a Monday.
        dates = services.get_next_start_dates(league, date(2026, 1, 5), count=3)

        self.assertEqual(len(dates), 3)
        for d in dates:
            self.assertEqual(d.weekday(), 2)  # Wednesday

    def test_skips_to_next_week_when_from_date_is_the_target_weekday(self):
        league = make_league(day_of_week=League.DayOfWeek.MONDAY)
        # 2026-01-05 is itself a Monday; the first result should be the following Monday.
        dates = services.get_next_start_dates(league, date(2026, 1, 5), count=1)
        self.assertEqual(dates[0], date(2026, 1, 12))


class SeedingTests(TestCase):
    def test_assign_random_team_seeds_assigns_unique_sequential_seeds(self):
        league = make_league()
        venue = make_venue(league)
        teams = make_teams(league, venue, 4)

        services.assign_random_team_seeds(league, random_seed=42)

        seeds = sorted(Team.objects.filter(league=league).values_list('seed', flat=True))
        self.assertEqual(seeds, [1, 2, 3, 4])

    def test_get_seeded_teams_orders_seeded_before_unseeded(self):
        league = make_league()
        venue = make_venue(league)
        seeded = make_team(league, venue, 'Seeded')
        unseeded = make_team(league, venue, 'Unseeded')
        seeded.seed = 1
        seeded.save(update_fields=['seed'])

        ordered = services.get_seeded_teams(league)
        self.assertEqual(ordered, [seeded, unseeded])


class RoundRobinPairingsTests(TestCase):
    def test_fewer_than_two_teams_returns_no_rounds(self):
        self.assertEqual(services.generate_round_robin_pairings([]), [])
        self.assertEqual(services.generate_round_robin_pairings(['only-one']), [])

    def test_even_team_count_plays_everyone_once(self):
        teams = ['A', 'B', 'C', 'D']
        rounds = services.generate_round_robin_pairings(teams, random_seed=1)

        self.assertEqual(len(rounds), 3)  # n - 1 rounds
        all_pairs = set()
        for round_matches in rounds:
            self.assertEqual(len(round_matches), 2)
            for home, away in round_matches:
                all_pairs.add(frozenset((home, away)))
        # 4 teams -> 6 unique pairings total.
        self.assertEqual(len(all_pairs), 6)

    def test_odd_team_count_gives_one_bye_per_round(self):
        teams = ['A', 'B', 'C']
        rounds = services.generate_round_robin_pairings(teams, random_seed=1)

        self.assertEqual(len(rounds), 3)
        for round_matches in rounds:
            self.assertEqual(len(round_matches), 1)


class WeekCanAcceptMatchTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.venue = make_venue(self.league, max_home_teams=1)
        self.team_a = make_team(self.league, self.venue, 'A')
        self.team_b = make_team(self.league, self.venue, 'B')
        self.team_c = make_team(self.league, self.venue, 'C')
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)

    def test_holiday_week_never_accepts_a_match(self):
        week = Week.objects.create(season=self.season, date=date(2026, 1, 1), number=None, notes='Holiday')
        self.assertFalse(services.week_can_accept_match(week, self.team_a, self.team_b))

    def test_rejects_when_team_already_playing_that_week(self):
        week = Week.objects.create(season=self.season, date=date(2026, 1, 1), number=1)
        Match.objects.create(week=week, home_team=self.team_a, away_team=self.team_b)
        self.assertFalse(services.week_can_accept_match(week, self.team_a, self.team_c))

    def test_rejects_when_home_venue_capacity_exceeded(self):
        other_home = make_team(self.league, self.venue, 'OtherHome')
        week = Week.objects.create(season=self.season, date=date(2026, 1, 1), number=1)
        Match.objects.create(week=week, home_team=other_home, away_team=self.team_c)

        unrelated_team = make_team(self.league, self.venue, 'Unrelated')
        self.assertFalse(services.week_can_accept_match(week, self.team_a, unrelated_team))

    def test_accepts_when_nothing_conflicts(self):
        week = Week.objects.create(season=self.season, date=date(2026, 1, 1), number=1)
        self.assertTrue(services.week_can_accept_match(week, self.team_a, self.team_b))


class RecreateSeasonScheduleTests(TestCase):
    def test_creates_weeks_and_matches_for_all_teams(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)

        weeks = services.recreate_season_schedule(season, date(2026, 1, 5), random_seed=1)

        self.assertGreaterEqual(len(weeks), 3)
        total_matches = Match.objects.filter(week__season=season).count()
        self.assertEqual(total_matches, 6)  # 4 teams round robin = 6 total matches

    def test_recreate_clears_previous_schedule(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)

        services.recreate_season_schedule(season, date(2026, 1, 5), random_seed=1)
        first_total = Match.objects.filter(week__season=season).count()

        services.recreate_season_schedule(season, date(2026, 2, 2), random_seed=2)
        second_total = Match.objects.filter(week__season=season).count()

        self.assertEqual(first_total, second_total)

    def test_returns_empty_for_fewer_than_two_teams(self):
        league = make_league()
        venue = make_venue(league)
        make_team(league, venue, 'Solo')
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)

        weeks = services.recreate_season_schedule(season, date(2026, 1, 5))
        self.assertEqual(weeks, [])

    def test_respects_holidays_by_creating_a_no_match_week(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        Holiday.objects.create(date=date(2026, 1, 5), description='New Year Break')

        services.recreate_season_schedule(season, date(2026, 1, 5), random_seed=1)

        holiday_week = season.weeks.get(date=date(2026, 1, 5))
        self.assertIsNone(holiday_week.number)
        self.assertEqual(holiday_week.notes, 'New Year Break')


class CreateMirroredSeasonScheduleTests(TestCase):
    def test_mirrors_each_match_with_home_and_away_swapped(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        services.recreate_season_schedule(season, date(2026, 1, 5), random_seed=1)
        original_total = Match.objects.filter(week__season=season).count()

        services.create_mirrored_season_schedule(season)

        mirrored_total = Match.objects.filter(week__season=season).count()
        self.assertEqual(mirrored_total, original_total * 2)

    def test_empty_season_returns_no_weeks(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        self.assertEqual(services.create_mirrored_season_schedule(season), [])


class GetValidDestinationWeeksTests(TestCase):
    def test_excludes_full_venue_weeks_and_holidays(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=1)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        other_home = make_team(league, venue, 'OtherHome')
        other_away = make_team(league, venue, 'OtherAway')

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        full_week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        Match.objects.create(week=full_week, home_team=other_home, away_team=other_away)
        open_week = Week.objects.create(season=season, date=date(2026, 1, 8), number=2)
        holiday_week = Week.objects.create(season=season, date=date(2026, 1, 15), number=None, notes='Holiday')

        # Placing this match in full_week too pushes that week further over
        # capacity; open_week remains the only week under the venue's limit.
        match = Match.objects.create(week=full_week, home_team=home, away_team=away, location=venue.name)

        valid_weeks = services.get_valid_destination_weeks(season, match)

        self.assertIn(open_week, valid_weeks)
        self.assertNotIn(holiday_week, valid_weeks)


class RenumberWeeksTests(TestCase):
    def test_renumbers_only_non_holiday_weeks_sequentially(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        Week.objects.create(season=season, date=date(2026, 1, 1), number=5)
        Week.objects.create(season=season, date=date(2026, 1, 8), number=None, notes='Holiday')
        Week.objects.create(season=season, date=date(2026, 1, 15), number=7)

        count = services.renumber_weeks(season)

        self.assertEqual(count, 2)
        numbers = list(season.weeks.order_by('date').values_list('number', flat=True))
        self.assertEqual(numbers, [1, None, 2])

    def test_empty_season_returns_zero(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        self.assertEqual(services.renumber_weeks(season), 0)


class DeleteWeekTests(TestCase):
    def test_raises_for_holiday_week(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=None, notes='Holiday')
        with self.assertRaises(ValueError):
            services.delete_week(week)

    def test_raises_when_matches_exist(self):
        league = make_league()
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        Match.objects.create(week=week, home_team=home, away_team=away)

        with self.assertRaises(ValueError):
            services.delete_week(week)

    def test_deletes_empty_playable_week(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)

        services.delete_week(week)
        self.assertFalse(Week.objects.filter(pk=week.pk).exists())


class MoveMatchToWeekTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.venue = make_venue(self.league, max_home_teams=1)
        self.home = make_team(self.league, self.venue, 'Home')
        self.away = make_team(self.league, self.venue, 'Away')
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.source_week = Week.objects.create(season=self.season, date=date(2026, 1, 1), number=1)
        self.match = Match.objects.create(week=self.source_week, home_team=self.home, away_team=self.away)

    def test_raises_for_different_season(self):
        other_league = make_league(name='Other')
        other_season = Season.objects.create(league=other_league, name='Other Season', status=Season.Status.ACTIVE)
        other_week = Week.objects.create(season=other_season, date=date(2026, 1, 8), number=1)

        with self.assertRaises(ValueError):
            services.move_match_to_week(self.match, other_week)

    def test_raises_for_holiday_target(self):
        holiday_week = Week.objects.create(season=self.season, date=date(2026, 1, 8), number=None, notes='Holiday')
        with self.assertRaises(ValueError):
            services.move_match_to_week(self.match, holiday_week)

    def test_raises_when_target_venue_capacity_exceeded(self):
        target_week = Week.objects.create(season=self.season, date=date(2026, 1, 8), number=2)
        other_home = make_team(self.league, self.venue, 'OtherHome')
        other_away = make_team(self.league, self.venue, 'OtherAway')
        Match.objects.create(week=target_week, home_team=other_home, away_team=other_away)

        with self.assertRaises(ValueError):
            services.move_match_to_week(self.match, target_week)

    def test_moves_match_successfully(self):
        target_week = Week.objects.create(season=self.season, date=date(2026, 1, 8), number=2)
        services.move_match_to_week(self.match, target_week)

        self.match.refresh_from_db()
        self.assertEqual(self.match.week_id, target_week.id)


class RebalanceSeasonMatchesTests(TestCase):
    def test_moves_overflow_match_to_a_future_week(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=1)
        home1 = make_team(league, venue, 'Home1')
        away1 = make_team(league, venue, 'Away1')
        home2 = make_team(league, venue, 'Home2')
        away2 = make_team(league, venue, 'Away2')

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        Week.objects.create(season=season, date=date(2026, 1, 8), number=2)

        Match.objects.create(week=week1, home_team=home1, away_team=away1, location=venue.name)
        overflow_match = Match.objects.create(week=week1, home_team=home2, away_team=away2, location=venue.name)

        moved = services.rebalance_season_matches(season)

        self.assertEqual(len(moved), 1)
        overflow_match.refresh_from_db()
        self.assertNotEqual(overflow_match.week_id, week1.id)

    def test_empty_season_returns_empty_list(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        self.assertEqual(services.rebalance_season_matches(season), [])


class ArchiveSeasonTests(TestCase):
    def test_raises_for_season_without_weeks(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        with self.assertRaises(ValueError):
            services.archive_season(season)

    def test_archives_one_pocket_season_with_matches_and_results(self):
        league = make_league(results_type=League.ResultsType.ONE_POCKET, team_size=1)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        MatchResult.objects.create(match=match, home_team_score=8, away_team_score=5)

        archived = services.archive_season(season)

        self.assertIsInstance(archived, ArchivedSeason)
        self.assertEqual(archived.teams.count(), 2)
        self.assertEqual(archived.players.count(), 2)
        self.assertEqual(archived.matches.count(), 1)
        archived_match = archived.matches.first()
        self.assertEqual(archived_match.home_team_score, 8)
        self.assertEqual(archived_match.away_team_score, 5)
        self.assertEqual(archived_match.player_results.count(), 2)
        self.assertFalse(Season.objects.filter(pk=season.pk).exists())

    def test_archives_eight_ball_season_without_match_archive(self):
        league = make_league(results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        player = Player.objects.create(league=league, name='Alice', team=home)

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        match_result = MatchResult.objects.create(match=match)
        PlayerMatchResult.objects.create(
            match_result=match_result,
            player=player,
            represented_team=home,
            wins=2,
        )

        archived = services.archive_season(season)

        self.assertEqual(archived.matches.count(), 0)
        archived_player = archived.players.get(player_name='Alice')
        self.assertEqual(archived_player.games_won, 2)
        self.assertEqual(archived_player.games_lost, 1)


class MoveWeekUpDownTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 1), number=1)
        self.week2 = Week.objects.create(season=self.season, date=date(2026, 1, 8), number=2)
        self.week3 = Week.objects.create(season=self.season, date=date(2026, 1, 15), number=3)

    def test_move_week_up_swaps_dates_and_renumbers(self):
        services.move_week_up(self.week2)

        self.week1.refresh_from_db()
        self.week2.refresh_from_db()
        self.assertEqual(self.week2.date, date(2026, 1, 1))
        self.assertEqual(self.week1.date, date(2026, 1, 8))
        self.assertEqual(self.week2.number, 1)
        self.assertEqual(self.week1.number, 2)

    def test_move_week_up_raises_for_first_week(self):
        with self.assertRaises(ValueError):
            services.move_week_up(self.week1)

    def test_move_week_down_swaps_dates_and_renumbers(self):
        services.move_week_down(self.week2)

        self.week2.refresh_from_db()
        self.week3.refresh_from_db()
        self.assertEqual(self.week2.date, date(2026, 1, 15))
        self.assertEqual(self.week3.date, date(2026, 1, 8))

    def test_move_week_down_raises_for_last_week(self):
        with self.assertRaises(ValueError):
            services.move_week_down(self.week3)


class CreateNewPlayableWeekAtEndTests(TestCase):
    def test_raises_without_existing_weeks(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        with self.assertRaises(ValueError):
            services.create_new_playable_week_at_end(season)

    def test_creates_a_new_playable_week_after_the_last_one(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        Week.objects.create(season=season, date=date(2026, 1, 1), number=1)

        new_week = services.create_new_playable_week_at_end(season)

        self.assertEqual(new_week.date, date(2026, 1, 8))
        self.assertEqual(new_week.number, 2)

    def test_skips_holiday_when_creating_new_week(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        Holiday.objects.create(date=date(2026, 1, 8), description='Holiday')

        new_week = services.create_new_playable_week_at_end(season)

        self.assertEqual(new_week.date, date(2026, 1, 15))
        self.assertEqual(new_week.number, 2)
