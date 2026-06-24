from django.test import TestCase

from core.models import League, Player
from scheduling.models import Season
from tournaments.bracket import (
    FLAT_PAYOUTS,
    LEAGUE_TOURNAMENT_CONTRIBUTION,
    PAYOUT_PERCENTAGES,
    PER_PLAYER_TOURNAMENT_CONTRIBUTION,
    compute_payouts,
    compute_placements,
    generate_bracket,
    set_winner,
)
from tournaments.models import BracketMatch, Tournament, TournamentTeam


def make_league(**kwargs):
    defaults = {
        'name': 'Test League',
        'team_size': 2,
        'results_type': League.ResultsType.ONE_POCKET,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_tournament(league):
    season = Season.objects.create(league=league, name='Season 1', status=Season.Status.WORKING)
    return Tournament.objects.create(season=season)


def make_teams(league, tournament, count):
    teams = []
    for i in range(1, count + 1):
        a_player = Player.objects.create(league=league, name=f'Player {i}A')
        b_player = Player.objects.create(league=league, name=f'Player {i}B')
        teams.append(
            TournamentTeam.objects.create(
                tournament=tournament,
                a_player=a_player,
                b_player=b_player,
                team_number=i,
            )
        )
    return teams


def play_out_bracket(tournament, pick=lambda match: match.team1):
    """Repeatedly resolve every currently-ready match by always picking `pick`
    (defaults to team1), until no ready matches remain."""
    while True:
        ready = list(BracketMatch.objects.filter(tournament=tournament, status=BracketMatch.STATUS_READY))
        if not ready:
            break
        for match in ready:
            match.refresh_from_db()
            if match.status != BracketMatch.STATUS_READY:
                continue
            set_winner(match, pick(match))


class GenerateBracketTests(TestCase):
    def test_two_teams_creates_winner_final_and_grand_final_only(self):
        league = make_league()
        tournament = make_tournament(league)
        make_teams(league, tournament, 2)

        generate_bracket(tournament)

        self.assertEqual(
            BracketMatch.objects.filter(tournament=tournament, bracket_side=BracketMatch.SIDE_WINNER).count(),
            1,
        )
        self.assertEqual(
            BracketMatch.objects.filter(tournament=tournament, bracket_side=BracketMatch.SIDE_LOSER).count(),
            0,
        )
        self.assertEqual(
            BracketMatch.objects.filter(tournament=tournament, bracket_side=BracketMatch.SIDE_FINAL).count(),
            1,
        )
        self.assertEqual(
            BracketMatch.objects.filter(tournament=tournament, bracket_side=BracketMatch.SIDE_RESET).count(),
            1,
        )

    def test_fewer_than_two_teams_creates_nothing(self):
        league = make_league()
        tournament = make_tournament(league)
        make_teams(league, tournament, 1)

        generate_bracket(tournament)

        self.assertEqual(BracketMatch.objects.filter(tournament=tournament).count(), 0)

    def test_regenerating_clears_previous_bracket(self):
        league = make_league()
        tournament = make_tournament(league)
        make_teams(league, tournament, 4)

        generate_bracket(tournament)
        first_count = BracketMatch.objects.filter(tournament=tournament).count()
        generate_bracket(tournament)
        second_count = BracketMatch.objects.filter(tournament=tournament).count()

        self.assertEqual(first_count, second_count)


class PlayBracketTests(TestCase):
    def test_two_team_bracket_resolves_to_clean_placements(self):
        league = make_league()
        tournament = make_tournament(league)
        team1, team2 = make_teams(league, tournament, 2)
        generate_bracket(tournament)

        play_out_bracket(tournament)

        tournament.refresh_from_db()
        self.assertIsNotNone(tournament.completed_at)

        placements = compute_placements(tournament)
        places = dict(placements)
        self.assertEqual(places[1], team1)
        self.assertEqual(places[2], team2)

    def test_four_team_bracket_produces_four_distinct_placements(self):
        league = make_league()
        tournament = make_tournament(league)
        make_teams(league, tournament, 4)
        generate_bracket(tournament)

        play_out_bracket(tournament)

        tournament.refresh_from_db()
        self.assertIsNotNone(tournament.completed_at)

        placements = compute_placements(tournament)
        places = [p for p, _team in placements]
        teams = [team for _p, team in placements]

        self.assertEqual(sorted(places), [1, 2, 3, 4])
        self.assertEqual(len(set(t.id for t in teams)), 4)


class ComputePayoutsTests(TestCase):
    def test_pool_math_for_four_teams_no_flat_places(self):
        league = make_league()
        tournament = make_tournament(league)
        make_teams(league, tournament, 4)
        placements = [(1, 'first'), (2, 'second'), (3, 'third'), (4, 'fourth')]

        payouts = compute_payouts(tournament, placements)

        expected_pool = LEAGUE_TOURNAMENT_CONTRIBUTION + PER_PLAYER_TOURNAMENT_CONTRIBUTION * 8
        self.assertEqual(payouts['pool'], expected_pool)
        self.assertEqual(payouts['player_count'], 8)
        self.assertEqual(payouts['flat_total'], 0)
        self.assertEqual(payouts['pool_after_flats'], expected_pool)

        amounts = {place: amount for place, _team, amount in payouts['rows']}
        self.assertAlmostEqual(amounts[1], expected_pool * PAYOUT_PERCENTAGES[1])
        self.assertAlmostEqual(amounts[2], expected_pool * PAYOUT_PERCENTAGES[2])
        self.assertAlmostEqual(amounts[3], expected_pool * PAYOUT_PERCENTAGES[3])
        self.assertAlmostEqual(amounts[4], expected_pool * PAYOUT_PERCENTAGES[4])

    def test_flat_payouts_for_seventh_and_eighth_place_come_off_the_top(self):
        league = make_league()
        tournament = make_tournament(league)
        make_teams(league, tournament, 8)
        placements = [(place, f'team{place}') for place in range(1, 9)]

        payouts = compute_payouts(tournament, placements)

        expected_pool = LEAGUE_TOURNAMENT_CONTRIBUTION + PER_PLAYER_TOURNAMENT_CONTRIBUTION * 16
        expected_flat_total = FLAT_PAYOUTS[7] + FLAT_PAYOUTS[8]
        self.assertEqual(payouts['pool'], expected_pool)
        self.assertEqual(payouts['flat_total'], expected_flat_total)
        self.assertEqual(payouts['pool_after_flats'], expected_pool - expected_flat_total)

        amounts = {place: amount for place, _team, amount in payouts['rows']}
        self.assertEqual(amounts[7], FLAT_PAYOUTS[7])
        self.assertEqual(amounts[8], FLAT_PAYOUTS[8])
        self.assertAlmostEqual(amounts[1], payouts['pool_after_flats'] * PAYOUT_PERCENTAGES[1])
