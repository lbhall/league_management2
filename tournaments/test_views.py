import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import League, Player, Team, Venue
from results.models import PlayerMatchResult, MatchResult
from scheduling.models import Match, Season, Week
from tournaments.bracket import generate_bracket, set_winner
from tournaments.models import BracketMatch, Tournament, TournamentPlayer, TournamentTeam


def make_league(**kwargs):
    defaults = {
        'name': 'Tournament League',
        'team_size': 1,
        'results_type': League.ResultsType.ONE_POCKET,
        'day_of_week': League.DayOfWeek.MONDAY,
        'tournament_target': 300,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Venue'):
    return Venue.objects.create(
        league=league, name=name, phone='555-0000', address='123 Main St',
        number_of_tables=2, max_home_teams=4, min_home_teams=1,
    )


def make_response(data):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(data).encode('utf-8')
    return response


def make_http_error(code, body):
    return urllib.error.HTTPError(
        'http://readysettourney.example/', code, 'error', {}, io.BytesIO(json.dumps(body).encode('utf-8')),
    )


class TournamentViewTestCase(TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'testserver'
        self.client.defaults['HTTP_USER_AGENT'] = 'test-agent'

        self.league = make_league()
        self.venue = make_venue(self.league)
        self.season = Season.objects.create(league=self.league, name='Season 1', status=Season.Status.ACTIVE)

        User = get_user_model()
        self.staff_user = User.objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.client.login(username='admin', password='password123')

    def make_eligible_players(self, count):
        """Players need >=2 PlayerMatchResult appearances to show up in tournament_players."""
        players = []
        day_offset = 1
        for i in range(count):
            team = Team.objects.create(league=self.league, venue=self.venue, name=f'Team {i}')
            player = Player.objects.create(league=self.league, name=f'Player {i}', team=team)
            for _ in range(2):
                week = Week.objects.create(
                    season=self.season, date=f'2026-01-{day_offset:02d}', number=day_offset,
                )
                day_offset += 1
                opponent_team = Team.objects.create(league=self.league, venue=self.venue, name=f'Opp {day_offset}')
                match = Match.objects.create(week=week, home_team=team, away_team=opponent_team)
                match_result = MatchResult.objects.create(match=match)
                PlayerMatchResult.objects.create(
                    match_result=match_result, player=player, represented_team=team, wins=1,
                )
            players.append(player)
        return players


class TournamentPlayersViewTests(TournamentViewTestCase):
    def test_redirects_home_without_active_league(self):
        with self.settings(FRONTEND_LEAGUE_ID=None):
            League.objects.all().delete()
            response = self.client.get(reverse('tournament_players'))
        self.assertRedirects(response, reverse('home'))

    def test_redirects_home_without_active_season(self):
        self.season.delete()
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('tournament_players'))
        self.assertRedirects(response, reverse('home'))

    def test_get_lists_eligible_players(self):
        players = self.make_eligible_players(2)
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('tournament_players'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, players[0].name)

    def test_select_players_then_make_teams(self):
        players = self.make_eligible_players(4)
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            self.client.post(reverse('tournament_players'), {
                'player_ids': [p.id for p in players],
            })
            self.assertEqual(TournamentPlayer.objects.count(), 4)

            response = self.client.post(reverse('tournament_players'), {'make_teams': '1'})
            self.assertRedirects(response, reverse('tournament_players'))
        self.assertEqual(TournamentTeam.objects.count(), 2)

    def test_toggle_paid(self):
        players = self.make_eligible_players(1)
        tournament = Tournament.objects.create(season=self.season)
        tp = TournamentPlayer.objects.create(tournament=tournament, player=players[0])

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            self.client.post(reverse('tournament_players'), {'toggle_paid': players[0].id})

        tp.refresh_from_db()
        self.assertTrue(tp.paid)

    def test_remove_player_clears_teams_and_bracket(self):
        players = self.make_eligible_players(2)
        tournament = Tournament.objects.create(season=self.season)
        for p in players:
            TournamentPlayer.objects.create(tournament=tournament, player=p)
        TournamentTeam.objects.create(tournament=tournament, a_player=players[0], b_player=players[1], team_number=1)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            self.client.post(reverse('tournament_players'), {'remove_player': players[0].id})

        self.assertEqual(TournamentPlayer.objects.filter(tournament=tournament).count(), 1)
        self.assertEqual(TournamentTeam.objects.filter(tournament=tournament).count(), 0)

    def test_clear_teams_and_clear_all(self):
        players = self.make_eligible_players(2)
        tournament = Tournament.objects.create(season=self.season)
        for p in players:
            TournamentPlayer.objects.create(tournament=tournament, player=p)
        TournamentTeam.objects.create(tournament=tournament, a_player=players[0], b_player=players[1], team_number=1)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            self.client.post(reverse('tournament_players'), {'clear_teams': '1'})
            self.assertEqual(TournamentTeam.objects.filter(tournament=tournament).count(), 0)

            self.client.post(reverse('tournament_players'), {'clear_all': '1'})
            self.assertEqual(TournamentPlayer.objects.filter(tournament=tournament).count(), 0)


class ExportTournamentTeamsTests(TournamentViewTestCase):
    def test_404_without_active_season(self):
        self.season.delete()
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('export_tournament_teams'))
        self.assertEqual(response.status_code, 404)

    def test_404_without_tournament(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('export_tournament_teams'))
        self.assertEqual(response.status_code, 404)

    def test_exports_csv_for_teams(self):
        players = self.make_eligible_players(2)
        tournament = Tournament.objects.create(season=self.season)
        TournamentTeam.objects.create(tournament=tournament, a_player=players[0], b_player=players[1], team_number=1)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('export_tournament_teams'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(players[0].name, response.content.decode())


class TournamentBracketViewTests(TournamentViewTestCase):
    def setUp(self):
        super().setUp()
        self.players = self.make_eligible_players(4)
        self.tournament = Tournament.objects.create(season=self.season)
        self.teams = [
            TournamentTeam.objects.create(
                tournament=self.tournament, a_player=self.players[i], b_player=self.players[i], team_number=i + 1,
            )
            for i in range(4)
        ]

    def test_redirects_to_players_without_tournament(self):
        self.tournament.delete()
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('tournament_bracket'))
        self.assertRedirects(response, reverse('tournament_players'))

    def test_generate_requires_teams(self):
        TournamentTeam.objects.all().delete()
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.post(reverse('tournament_bracket'), {'generate': '1'})
        self.assertRedirects(response, reverse('tournament_players'))

    def test_full_bracket_flow(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.post(reverse('tournament_bracket'), {'generate': '1'})
            self.assertRedirects(response, reverse('tournament_bracket'))

            response = self.client.get(reverse('tournament_bracket'))
            self.assertEqual(response.status_code, 200)

            ready_match = BracketMatch.objects.filter(
                tournament=self.tournament, status=BracketMatch.STATUS_READY,
            ).first()
            response = self.client.post(reverse('tournament_bracket'), {
                'match_id': ready_match.id, 'winner_id': ready_match.team1_id,
            })
            self.assertRedirects(response, reverse('tournament_bracket'))

            ready_match.refresh_from_db()
            self.assertEqual(ready_match.status, BracketMatch.STATUS_COMPLETE)

            response = self.client.post(reverse('tournament_bracket'), {'undo_match_id': ready_match.id})
            self.assertRedirects(response, reverse('tournament_bracket'))
            ready_match.refresh_from_db()
            self.assertEqual(ready_match.status, BracketMatch.STATUS_READY)

            response = self.client.post(reverse('tournament_bracket'), {'mark_complete': '1'})
            self.tournament.refresh_from_db()
            self.assertIsNotNone(self.tournament.completed_at)

            response = self.client.post(reverse('tournament_bracket'), {'mark_in_progress': '1'})
            self.tournament.refresh_from_db()
            self.assertIsNone(self.tournament.completed_at)

            response = self.client.post(reverse('tournament_bracket'), {'clear_bracket': '1'})
            self.assertEqual(BracketMatch.objects.filter(tournament=self.tournament).count(), 0)

    def test_invalid_winner_rejected(self):
        generate_bracket(self.tournament)
        ready_match = BracketMatch.objects.filter(
            tournament=self.tournament, status=BracketMatch.STATUS_READY,
        ).first()
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.post(reverse('tournament_bracket'), {
                'match_id': ready_match.id, 'winner_id': 999999,
            })
        self.assertRedirects(response, reverse('tournament_bracket'))
        ready_match.refresh_from_db()
        self.assertNotEqual(ready_match.status, BracketMatch.STATUS_COMPLETE)

    def test_already_complete_match_rejected(self):
        generate_bracket(self.tournament)
        ready_match = BracketMatch.objects.filter(
            tournament=self.tournament, status=BracketMatch.STATUS_READY,
        ).first()
        set_winner(ready_match, ready_match.team1)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.post(reverse('tournament_bracket'), {
                'match_id': ready_match.id, 'winner_id': ready_match.team1_id,
            })
        self.assertRedirects(response, reverse('tournament_bracket'))


class EndOfSeasonTournamentViewTests(TournamentViewTestCase):
    def test_no_completed_tournaments_renders_empty(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('end_of_season_tournament'))
        self.assertEqual(response.status_code, 200)

    def test_completed_tournament_renders_bracket(self):
        players = self.make_eligible_players(2)
        tournament = Tournament.objects.create(season=self.season)
        TournamentTeam.objects.create(tournament=tournament, a_player=players[0], b_player=players[0], team_number=1)
        TournamentTeam.objects.create(tournament=tournament, a_player=players[1], b_player=players[1], team_number=2)
        generate_bracket(tournament)
        from django.utils import timezone
        tournament.completed_at = timezone.now()
        tournament.save(update_fields=['completed_at'])

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('end_of_season_tournament'))
            self.assertEqual(response.status_code, 200)

            response = self.client.get(reverse('end_of_season_tournament_detail', args=[tournament.id]))
            self.assertEqual(response.status_code, 200)


class CreateReadySetTourneyTournamentViewTests(TournamentViewTestCase):
    def setUp(self):
        super().setUp()
        self.players = self.make_eligible_players(2)
        self.tournament = Tournament.objects.create(season=self.season)
        TournamentTeam.objects.create(
            tournament=self.tournament, a_player=self.players[0], b_player=self.players[1], team_number=1,
        )

    def test_requires_post(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('create_readysettourney_tournament'))
        self.assertEqual(response.status_code, 405)

    def test_no_teams_to_send(self):
        TournamentTeam.objects.all().delete()
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))

    def test_missing_credentials(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id, ONTHEHILL_USERNAME='', ONTHEHILL_PASSWORD=''):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_successful_creation_with_payouts(self, mock_urlopen):
        mock_urlopen.side_effect = [
            make_response({'token': 'abc123'}),
            make_response({'id': 42, 'url': 'http://readysettourney.example/t/42'}),
        ] + [make_response({}) for _ in range(8)]  # 6 percentage + 2 flat payouts

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'), {
                'name': 'Test Tournament', 'entry_fee': '20',
            })
        self.assertRedirects(response, reverse('tournament_players'))
        self.assertEqual(mock_urlopen.call_count, 10)

    @patch('tournaments.views.urllib.request.urlopen')
    def test_token_fetch_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = make_http_error(401, {'error': 'bad credentials'})

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='wrong',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_token_fetch_url_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError('connection refused')

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_tournament_creation_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = [
            make_response({'token': 'abc123'}),
            make_http_error(400, {'error': 'bad request'}),
        ]

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'), {'venue_id': '7'})
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_invalid_venue_id(self, mock_urlopen):
        mock_urlopen.side_effect = [make_response({'token': 'abc123'})]

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'), {'venue_id': 'not-a-number'})
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_payout_post_errors_are_collected_but_dont_fail_request(self, mock_urlopen):
        mock_urlopen.side_effect = [
            make_response({'token': 'abc123'}),
            make_response({'id': 42, 'url': 'http://readysettourney.example/t/42'}),
        ] + [make_http_error(409, {'error': 'duplicate place'}) for _ in range(8)]

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_missing_tournament_id_in_response(self, mock_urlopen):
        mock_urlopen.side_effect = [
            make_response({'token': 'abc123'}),
            make_response({'url': 'http://readysettourney.example/t/42'}),  # no 'id'
        ]

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))

    @patch('tournaments.views.urllib.request.urlopen')
    def test_missing_token_in_response(self, mock_urlopen):
        mock_urlopen.side_effect = [make_response({})]  # no 'token'

        with self.settings(
            FRONTEND_LEAGUE_ID=self.league.id,
            ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret',
        ):
            response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'))
