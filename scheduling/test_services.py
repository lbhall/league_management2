from datetime import date, timedelta

from django.db.models import Q
from django.test import TestCase

from core.models import League, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling import services
from scheduling.models import (
    ArchivedMatch,
    ArchivedPlayer,
    ArchivedPlayerMatchResult,
    ArchivedTeam,
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
        make_teams(league, venue, 4)

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

    def test_mirror_placed_into_later_existing_week_is_not_mirrored_again(self):
        """Regression: a mirrored match may legitimately land in a later
        *existing* week with spare capacity. The loop must not pick that new
        match up when it reaches that week and mirror it a second time,
        duplicating the original fixture."""
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        team_a, team_b = make_teams(league, venue, 2)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        Week.objects.create(season=season, date=date(2026, 1, 12), number=2)  # empty, has capacity
        Match.objects.create(week=week1, home_team=team_a, away_team=team_b, location=venue.name)

        services.create_mirrored_season_schedule(season)

        matches = Match.objects.filter(week__season=season)
        self.assertEqual(matches.count(), 2)
        self.assertEqual(matches.filter(home_team=team_a, away_team=team_b).count(), 1)
        self.assertEqual(matches.filter(home_team=team_b, away_team=team_a).count(), 1)


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
        home.team_rank = 5
        home.save()
        away.team_rank = 4
        away.save()

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        week2 = Week.objects.create(season=season, date=date(2026, 1, 8), number=2)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        MatchResult.objects.create(match=match, home_team_score=8, away_team_score=5)
        # Never played before the archive.
        Match.objects.create(week=week2, home_team=away, away_team=home)

        archived = services.archive_season(season)

        self.assertIsInstance(archived, ArchivedSeason)
        self.assertEqual(archived.results_type, League.ResultsType.ONE_POCKET)
        self.assertEqual(archived.team_size, 1)
        self.assertEqual(archived.start_date, date(2026, 1, 1))
        self.assertEqual(archived.end_date, date(2026, 1, 8))

        self.assertEqual(archived.teams.count(), 2)
        home_row = archived.teams.get(team_name='Home')
        away_row = archived.teams.get(team_name='Away')
        self.assertEqual(home_row.league_rank, 1)
        self.assertEqual(away_row.league_rank, 2)
        self.assertEqual(home_row.team_rank, 5)
        self.assertEqual(away_row.team_rank, 4)
        self.assertEqual(home_row.captain_name, 'Home')
        self.assertEqual(home_row.team, home)
        self.assertEqual(home_row.venue_name, venue.name)

        self.assertEqual(archived.players.count(), 2)
        self.assertEqual(archived.matches.count(), 2)

        played = archived.matches.get(date=date(2026, 1, 1))
        self.assertEqual(played.week_number, 1)
        self.assertEqual(played.home_team_score, 8)
        self.assertEqual(played.away_team_score, 5)
        self.assertEqual(played.home_team_rank, 5)
        self.assertEqual(played.away_team_rank, 4)
        self.assertEqual(played.home_race, 9)
        self.assertEqual(played.away_race, 7)
        self.assertEqual(played.race_label, '9/7')
        self.assertTrue(played.completed)
        self.assertIsNotNone(played.result_recorded_at)
        self.assertEqual(played.player_results.count(), 2)
        home_line = played.player_results.get(team_name='Home')
        self.assertEqual(home_line.player, Player.objects.get(league=league, name='Home'))

        unplayed = archived.matches.get(date=date(2026, 1, 8))
        self.assertIsNone(unplayed.home_team_score)
        self.assertFalse(unplayed.completed)
        self.assertIsNone(unplayed.result_recorded_at)
        self.assertEqual(unplayed.player_results.count(), 0)

        # Soft archive: the season is kept, marked archived, and linked.
        season.refresh_from_db()
        self.assertEqual(season.status, Season.Status.ARCHIVED)
        self.assertEqual(archived.season, season)
        self.assertEqual(season.archive, archived)

    def test_rearchiving_rebuilds_the_snapshot_from_live_data(self):
        league = make_league(results_type=League.ResultsType.ONE_POCKET, team_size=1)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        result = MatchResult.objects.create(match=match, home_team_score=3, away_team_score=1)

        first = services.archive_season(season)
        self.assertEqual(first.teams.get(team_name='Home').matches_won, 1)

        # Fix a score after archiving, then rebuild.
        result.home_team_score = 1
        result.away_team_score = 3
        result.save()

        season.refresh_from_db()
        rebuilt = services.archive_season(season)

        self.assertFalse(ArchivedSeason.objects.filter(pk=first.pk).exists())
        self.assertEqual(ArchivedSeason.objects.filter(league=league).count(), 1)
        self.assertEqual(rebuilt.season, season)
        self.assertEqual(rebuilt.teams.get(team_name='Home').matches_won, 0)
        self.assertEqual(rebuilt.teams.get(team_name='Away').league_rank, 1)
        season.refresh_from_db()
        self.assertEqual(season.status, Season.Status.ARCHIVED)

    def test_working_season_cannot_be_archived(self):
        league = make_league()
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        with self.assertRaises(ValueError):
            services.archive_season(season)

    def test_archives_eight_ball_season_with_matches_and_player_results(self):
        league = make_league(results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')
        captain = Player.objects.create(league=league, name='Alice', team=home, male=False)
        home.captain = captain
        home.save()
        bob = Player.objects.create(league=league, name='Bob', team=away)

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        week2 = Week.objects.create(season=season, date=date(2026, 1, 8), number=2)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        match_result = MatchResult.objects.create(match=match)
        PlayerMatchResult.objects.create(
            match_result=match_result,
            player=captain,
            represented_team=home,
            wins=2,
            runouts=1,
        )
        PlayerMatchResult.objects.create(
            match_result=match_result,
            player=bob,
            represented_team=away,
            wins=3,
            eight_on_the_breaks=1,
        )
        Match.objects.create(week=week2, home_team=away, away_team=home)

        archived = services.archive_season(season)

        self.assertEqual(archived.results_type, League.ResultsType.EIGHT_BALL)
        self.assertEqual(archived.team_size, 3)

        home_row = archived.teams.get(team_name='Home')
        self.assertEqual(home_row.captain_name, 'Alice')
        self.assertIsNone(home_row.team_rank)

        archived_player = archived.players.get(player_name='Alice')
        self.assertEqual(archived_player.games_won, 2)
        self.assertEqual(archived_player.games_lost, 1)
        self.assertFalse(archived_player.male)
        # Linked by id, so the stats follow the player across seasons.
        self.assertEqual(archived_player.player, captain)
        self.assertEqual(home_row.team, home)

        # 8-ball now keeps every match plus each player's line for it.
        self.assertEqual(archived.matches.count(), 2)
        played = archived.matches.get(date=date(2026, 1, 1))
        self.assertTrue(played.completed)
        self.assertEqual(played.home_team_score, 2)
        self.assertEqual(played.away_team_score, 3)
        self.assertIsNone(played.home_race)
        self.assertEqual(played.player_results.count(), 2)
        alice_line = played.player_results.get(player_name='Alice')
        self.assertEqual(alice_line.player, captain)
        self.assertEqual(alice_line.team_name, 'Home')
        self.assertEqual(alice_line.wins, 2)
        self.assertEqual(alice_line.losses, 1)
        self.assertEqual(alice_line.runouts, 1)
        bob_line = played.player_results.get(player_name='Bob')
        self.assertEqual(bob_line.eight_on_the_breaks, 1)
        self.assertTrue(bob_line.won_all_games)

        unplayed = archived.matches.get(date=date(2026, 1, 8))
        self.assertFalse(unplayed.completed)
        self.assertEqual(unplayed.player_results.count(), 0)

    def test_darts_season_archives_standings_only(self):
        league = make_league(results_type=League.ResultsType.DARTS, team_size=2)
        venue = make_venue(league)
        home = make_team(league, venue, 'Home')
        away = make_team(league, venue, 'Away')

        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 1), number=1)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        MatchResult.objects.create(match=match, home_team_score=5, away_team_score=3)

        archived = services.archive_season(season)

        self.assertEqual(archived.teams.count(), 2)
        self.assertEqual(archived.matches.count(), 0)


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


class CompressSeasonMatchesTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.venue = make_venue(self.league, max_home_teams=4)
        self.team_a, self.team_b, self.team_c, self.team_d = make_teams(self.league, self.venue, 4)
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)

    def week(self, day, number):
        return Week.objects.create(season=self.season, date=day, number=number)

    def match(self, week, home, away, location=None):
        return Match.objects.create(
            week=week, home_team=home, away_team=away,
            location=location or home.venue.name)

    def test_moves_match_to_earliest_week_and_drops_emptied_trailing_week(self):
        week1 = self.week(date(2026, 1, 5), 1)
        week2 = self.week(date(2026, 1, 12), 2)
        self.match(week1, self.team_a, self.team_b)
        late = self.match(week2, self.team_c, self.team_d)

        moved, deleted = services.compress_season_matches(self.season)

        self.assertEqual(moved, 1)
        self.assertEqual(deleted, 1)
        late.refresh_from_db()
        self.assertEqual(late.week_id, week1.id)
        self.assertEqual(self.season.weeks.count(), 1)

    def test_respects_team_conflicts(self):
        week1 = self.week(date(2026, 1, 5), 1)
        week2 = self.week(date(2026, 1, 12), 2)
        self.match(week1, self.team_a, self.team_b)
        conflicted = self.match(week2, self.team_a, self.team_c)

        moved, deleted = services.compress_season_matches(self.season)

        self.assertEqual((moved, deleted), (0, 0))
        conflicted.refresh_from_db()
        self.assertEqual(conflicted.week_id, week2.id)

    def test_respects_venue_capacity(self):
        tight_venue = make_venue(self.league, name='Tight', max_home_teams=1)
        home1 = make_team(self.league, tight_venue, 'Tight 1')
        home2 = make_team(self.league, tight_venue, 'Tight 2')
        week1 = self.week(date(2026, 1, 5), 1)
        week2 = self.week(date(2026, 1, 12), 2)
        self.match(week1, home1, self.team_a)
        capped = self.match(week2, home2, self.team_b)

        moved, deleted = services.compress_season_matches(self.season)

        self.assertEqual((moved, deleted), (0, 0))
        capped.refresh_from_db()
        self.assertEqual(capped.week_id, week2.id)

    def test_capacity_uses_location_override_not_home_venue(self):
        # The moving match nominally belongs to self.venue but is played at
        # Overflow Hall (location override), which is empty in week 1, so
        # capacity is judged there and the move is allowed.
        make_venue(self.league, name='Overflow Hall', max_home_teams=1)
        week1 = self.week(date(2026, 1, 5), 1)
        week2 = self.week(date(2026, 1, 12), 2)
        self.match(week1, self.team_a, self.team_b)
        relocated = self.match(week2, self.team_c, self.team_d, location='Overflow Hall')

        moved, deleted = services.compress_season_matches(self.season)

        self.assertEqual(moved, 1)
        relocated.refresh_from_db()
        self.assertEqual(relocated.week_id, week1.id)

    def test_middle_empty_week_is_kept_only_trailing_deleted(self):
        week1 = self.week(date(2026, 1, 5), 1)
        week2 = self.week(date(2026, 1, 12), 2)   # empty, mid-season
        week3 = self.week(date(2026, 1, 19), 3)
        self.match(week1, self.team_a, self.team_b)
        self.match(week3, self.team_a, self.team_c)  # conflict: cannot move to week1

        moved, deleted = services.compress_season_matches(self.season)

        self.assertEqual(moved, 1)   # moves from week3 to the empty week2
        self.assertEqual(deleted, 1)  # week3 emptied and trailing
        self.assertEqual(
            list(self.season.weeks.order_by('date').values_list('id', flat=True)),
            [week1.id, week2.id])

    def test_holiday_weeks_are_ignored(self):
        week1 = self.week(date(2026, 1, 5), 1)
        Week.objects.create(season=self.season, date=date(2026, 1, 12), number=None, notes='Holiday')
        week3 = self.week(date(2026, 1, 19), 2)
        self.match(week1, self.team_a, self.team_b)
        self.match(week3, self.team_c, self.team_d)

        moved, deleted = services.compress_season_matches(self.season)

        self.assertEqual(moved, 1)
        self.assertEqual(deleted, 1)
        self.assertTrue(self.season.weeks.filter(number__isnull=True).exists())


class MirrorCompressionIntegrationTests(TestCase):
    def test_mirror_compresses_and_drops_leftover_empty_weeks(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        team_a, team_b = make_teams(league, venue, 2)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        Week.objects.create(season=season, date=date(2026, 1, 12), number=2)  # spare
        Week.objects.create(season=season, date=date(2026, 1, 19), number=3)  # spare
        Match.objects.create(week=week1, home_team=team_a, away_team=team_b, location=venue.name)

        services.create_mirrored_season_schedule(season)

        # mirror lands in week 2; week 3 is empty and should be dropped
        self.assertEqual(season.weeks.count(), 2)
        self.assertEqual(Match.objects.filter(week__season=season).count(), 2)

    def test_mirror_returns_only_surviving_created_weeks(self):
        league = make_league()
        venue = make_venue(league, max_home_teams=4)
        team_a, team_b = make_teams(league, venue, 2)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        Match.objects.create(week=week1, home_team=team_a, away_team=team_b, location=venue.name)

        created = services.create_mirrored_season_schedule(season)

        # the mirror needs a new week (team conflict in week 1), which survives compression
        self.assertEqual(len(created), 1)
        self.assertEqual(season.weeks.count(), 2)


class PackSeasonScheduleTests(TestCase):
    """pack_season_schedule: optimal repacking of a working season."""

    def _build_emc_like_double_round_robin(self):
        """9 teams, two venues capped at 2/night (5 teams at one venue):
        the real EMC shape. Full double round robin spread naively, one
        match per week, 72 weeks."""
        league = make_league(name='Pack League')
        hd = make_venue(league, name='Half Dollar', max_home_teams=2)
        cc = make_venue(league, name='Cue Club', max_home_teams=2)
        teams = [make_team(league, hd if i < 5 else cc, f'Team {i}') for i in range(9)]
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        day = date(2026, 1, 5)
        number = 1
        for i, home in enumerate(teams):
            for away in teams[i + 1:]:
                for h, a in ((home, away), (away, home)):
                    week = Week.objects.create(season=season, date=day, number=number)
                    Match.objects.create(week=week, home_team=h, away_team=a, location=h.venue.name)
                    day += timedelta(weeks=1)
                    number += 1
        return league, season, teams

    def test_packs_to_the_venue_floor_with_balanced_weeks(self):
        league, season, teams = self._build_emc_like_double_round_robin()

        result = services.pack_season_schedule(season, random_seed=1)

        weeks = list(season.weeks.filter(number__isnull=False).order_by('date'))
        self.assertEqual(len(weeks), 20)  # 5 teams x 8 home games / 2 per night
        sizes = sorted((w.matches.count() for w in weeks), reverse=True)
        self.assertEqual(sizes, [4] * 12 + [3] * 8)
        self.assertTrue(result['optimal'])
        # constraints: team once a week, <=2 matches per venue per week
        for week in weeks:
            seen = set()
            per_venue = {}
            for match in week.matches.select_related('home_team__venue'):
                self.assertNotIn(match.home_team_id, seen)
                self.assertNotIn(match.away_team_id, seen)
                seen |= {match.home_team_id, match.away_team_id}
                venue = services._match_effective_venue(match)
                per_venue[venue.id] = per_venue.get(venue.id, 0) + 1
            for venue_id, count in per_venue.items():
                self.assertLessEqual(count, 2)

    def test_no_back_to_back_byes_after_polish(self):
        league, season, teams = self._build_emc_like_double_round_robin()
        services.pack_season_schedule(season, random_seed=1)

        weeks = list(season.weeks.filter(number__isnull=False).order_by('date'))
        for team in teams:
            bye_indexes = [
                i for i, week in enumerate(weeks)
                if not week.matches.filter(Q(home_team=team) | Q(away_team=team)).exists()
            ]
            for x, y in zip(bye_indexes, bye_indexes[1:]):
                self.assertNotEqual(y - x, 1, f'{team} idle twice in a row at weeks {x+1},{y+1}')

    def test_falls_back_to_greedy_when_search_budget_exhausted(self):
        league = make_league(name='Fallback League')
        venue = make_venue(league, max_home_teams=4)
        team_a, team_b, team_c, team_d = make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        week2 = Week.objects.create(season=season, date=date(2026, 1, 12), number=2)
        Match.objects.create(week=week1, home_team=team_a, away_team=team_b, location=venue.name)
        late = Match.objects.create(week=week2, home_team=team_c, away_team=team_d, location=venue.name)

        result = services.pack_season_schedule(season, random_seed=1, attempts=0)

        self.assertFalse(result['optimal'])
        late.refresh_from_db()
        self.assertEqual(late.week_id, week1.id)  # greedy fallback still compressed
        self.assertEqual(season.weeks.count(), 1)

    def test_refuses_to_move_matches_once_results_exist(self):
        league = make_league(name='Played League')
        venue = make_venue(league, max_home_teams=4)
        team_a, team_b, team_c, team_d = make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        week2 = Week.objects.create(season=season, date=date(2026, 1, 12), number=2)
        played = Match.objects.create(week=week1, home_team=team_a, away_team=team_b, location=venue.name)
        MatchResult.objects.create(match=played, home_team_score=5, away_team_score=3)
        movable = Match.objects.create(week=week2, home_team=team_c, away_team=team_d, location=venue.name)

        result = services.pack_season_schedule(season, random_seed=1)

        self.assertEqual(result, {'optimal': False, 'moved': 0, 'deleted_weeks': 0, 'week_count': 2})
        movable.refresh_from_db()
        self.assertEqual(movable.week_id, week2.id)

    def test_greedy_compress_also_refuses_once_results_exist(self):
        league = make_league(name='Played League 2')
        venue = make_venue(league, max_home_teams=4)
        team_a, team_b, team_c, team_d = make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.ACTIVE)
        week1 = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        week2 = Week.objects.create(season=season, date=date(2026, 1, 12), number=2)
        played = Match.objects.create(week=week1, home_team=team_a, away_team=team_b, location=venue.name)
        MatchResult.objects.create(match=played, home_team_score=5, away_team_score=3)
        Match.objects.create(week=week2, home_team=team_c, away_team=team_d, location=venue.name)

        self.assertEqual(services.compress_season_matches(season), (0, 0))
        self.assertEqual(season.weeks.count(), 2)


class ArchiveLinkingTests(TestCase):
    def setUp(self):
        self.league = make_league(results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        venue = make_venue(self.league)
        self.home = make_team(self.league, venue, 'Home')
        self.alice = Player.objects.create(league=self.league, name='Alice', team=self.home)
        # Two live players share a name: ambiguous, must not be linked.
        Player.objects.create(league=self.league, name='Justin', team=self.home)
        Player.objects.create(league=self.league, name='Justin')

        self.archived = ArchivedSeason.objects.create(
            league=self.league, name='2024', results_type=League.ResultsType.EIGHT_BALL, team_size=3,
        )
        self.team_row = ArchivedTeam.objects.create(archived_season=self.archived, team_name='home')
        self.alice_row = ArchivedPlayer.objects.create(
            archived_season=self.archived, player_name='ALICE ', team_name='Home', games_won=4, games_lost=2,
        )
        self.justin_row = ArchivedPlayer.objects.create(archived_season=self.archived, player_name='Justin')
        self.gone_row = ArchivedPlayer.objects.create(archived_season=self.archived, player_name='Nobody')
        match = ArchivedMatch.objects.create(
            archived_season=self.archived, date=date(2024, 1, 1), home_team_name='Home', away_team_name='Away',
        )
        self.alice_line = ArchivedPlayerMatchResult.objects.create(
            archived_match=match, player_name='Alice', team_name='Home', wins=2, losses=1,
        )

    def test_links_unique_names_only(self):
        counts = services.link_archived_season_by_name(self.archived)
        self.assertEqual(counts, (1, 1, 1))

        self.team_row.refresh_from_db()
        self.alice_row.refresh_from_db()
        self.justin_row.refresh_from_db()
        self.gone_row.refresh_from_db()
        self.alice_line.refresh_from_db()
        self.assertEqual(self.team_row.team, self.home)
        self.assertEqual(self.alice_row.player, self.alice)
        self.assertEqual(self.alice_line.player, self.alice)
        self.assertIsNone(self.justin_row.player)
        self.assertIsNone(self.gone_row.player)

    def test_does_not_overwrite_existing_links(self):
        other = Player.objects.create(league=self.league, name='Alice Other')
        self.alice_row.player = other
        self.alice_row.save()
        services.link_archived_season_by_name(self.archived)
        self.alice_row.refresh_from_db()
        self.assertEqual(self.alice_row.player, other)

    def test_other_league_players_are_never_linked(self):
        other_league = make_league(name='Other', results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        Player.objects.create(league=other_league, name='Nobody')
        services.link_archived_season_by_name(self.archived)
        self.gone_row.refresh_from_db()
        self.assertIsNone(self.gone_row.player)

    def test_player_season_history_and_career_totals(self):
        older = ArchivedSeason.objects.create(
            league=self.league, name='2023', results_type=League.ResultsType.EIGHT_BALL, team_size=3,
            start_date=date(2023, 1, 1),
        )
        self.archived.start_date = date(2024, 1, 1)
        self.archived.save()
        ArchivedPlayer.objects.create(
            archived_season=older, player_name='Alice', player=self.alice, games_won=6, games_lost=4, run_outs=1,
        )
        self.alice_row.player = self.alice
        self.alice_row.save()

        rows, totals = services.build_player_season_history(self.alice)
        self.assertEqual([r.archived_season.name for r in rows], ['2024', '2023'])
        self.assertEqual(totals['games_won'], 10)
        self.assertEqual(totals['games_lost'], 6)
        self.assertEqual(totals['run_outs'], 1)
        self.assertAlmostEqual(totals['percentage'], 62.5)

    def test_deleting_a_player_keeps_the_archive_row(self):
        self.alice_row.player = self.alice
        self.alice_row.save()
        self.alice.delete()
        self.alice_row.refresh_from_db()
        self.assertIsNone(self.alice_row.player)
        self.assertEqual(self.alice_row.player_name, 'ALICE ')
