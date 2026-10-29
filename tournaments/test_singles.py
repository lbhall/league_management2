import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import League, LeagueAdminAccess, Player, Team
from results.models import MatchResult
from scheduling.models import Match, Season, Week
from tournaments.models import TournamentPlayer, Tournament
from tournaments.test_views import TournamentViewTestCase, make_league, make_response, make_venue


class TournamentLeagueChoiceTests(TournamentViewTestCase):
    def setUp(self):
        super().setUp()
        self.one_pocket = make_league(
            name='One Pocket League', team_size=1, results_type=League.ResultsType.ONE_POCKET,
        )
        Season.objects.create(league=self.one_pocket, name='OP Season', status=Season.Status.ACTIVE)
        make_league(name='Darts League', team_size=2, results_type=League.ResultsType.DARTS)

    def test_admin_with_several_leagues_must_choose(self):
        response = self.client.get(reverse('tournament_players'))
        self.assertTemplateUsed(response, 'tournaments/tournament_league_choice.html')
        self.assertContains(response, f'data-tournament-league="{self.league.pk}"')
        self.assertContains(response, f'data-tournament-league="{self.one_pocket.pk}"')
        self.assertNotContains(response, 'Darts League')

    def test_post_without_a_league_changes_nothing(self):
        players = self.make_eligible_players(1)
        response = self.client.post(reverse('tournament_players'), {'player_ids': [players[0].id]})
        self.assertTemplateUsed(response, 'tournaments/tournament_league_choice.html')
        self.assertFalse(TournamentPlayer.objects.exists())

    def test_choosing_a_league_opens_it(self):
        response = self.client.get(self.players_url)
        self.assertTemplateUsed(response, 'tournaments/tournament_players.html')
        self.assertEqual(response.context['active_league'], self.league)
        self.assertContains(response, 'data-change-league')

    def test_league_scoped_admin_only_gets_their_league(self):
        scoped = get_user_model().objects.create_user(
            username='op_admin', password='password123', is_staff=True,
        )
        LeagueAdminAccess.objects.create(user=scoped, league=self.one_pocket)
        self.client.logout()
        self.client.login(username='op_admin', password='password123')

        response = self.client.get(self.players_url)  # asks for the 8-ball league
        self.assertEqual(response.context['active_league'], self.one_pocket)
        self.assertNotContains(response, 'data-change-league')

    def test_other_tournament_pages_send_an_unchosen_league_to_the_chooser(self):
        response = self.client.get(reverse('tournament_bracket'))
        self.assertRedirects(response, reverse('tournament_players'), fetch_redirect_response=False)
        response = self.client.post(reverse('create_readysettourney_tournament'))
        self.assertRedirects(response, reverse('tournament_players'), fetch_redirect_response=False)


class SinglesTournamentTests(TestCase):
    """One-pocket tournaments: single players, sent straight to ReadySetTourney."""

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'testserver'
        self.client.defaults['HTTP_USER_AGENT'] = 'test-agent'
        self.league = make_league(name='Bogies', team_size=1, results_type=League.ResultsType.ONE_POCKET)
        self.venue = make_venue(self.league)
        self.season = Season.objects.create(league=self.league, name='Mid 2026', status=Season.Status.ACTIVE)
        get_user_model().objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.client.login(username='admin', password='password123')
        self.players_url = f"{reverse('tournament_players')}?league={self.league.pk}"
        self.send_url = f"{reverse('create_readysettourney_tournament')}?league={self.league.pk}"

        self.players = {}
        for name in ('Abel', 'Carl', 'Jim', 'Lupe'):
            team = Team.objects.create(league=self.league, venue=self.venue, name=name)
            self.players[name] = Player.objects.create(league=self.league, name=name, team=team)
        self.week_number = 0
        # Jim 2-0, Abel 1-1, Carl 1-1, Lupe 0-2; Lupe and Abel also have an unfinished match.
        self.play('Jim', 'Abel', 3, 1)
        self.play('Abel', 'Lupe', 3, 0)
        self.play('Carl', 'Jim', 2, 3)
        self.play('Carl', 'Lupe', 3, 2)
        self.play('Lupe', 'Abel', 1, 1)

    def play(self, home, away, home_score, away_score):
        self.week_number += 1
        week = Week.objects.create(
            season=self.season, date=f'2026-02-{self.week_number:02d}', number=self.week_number,
        )
        home_player, away_player = self.players[home], self.players[away]
        match = Match.objects.create(week=week, home_team=home_player.team, away_team=away_player.team)
        MatchResult.objects.create(
            match=match, home_player=home_player, away_player=away_player,
            home_team_score=home_score, away_team_score=away_score,
        )

    def enter(self, *names):
        tournament, _ = Tournament.objects.get_or_create(season=self.season)
        for name in names:
            TournamentPlayer.objects.create(tournament=tournament, player=self.players[name])
        return tournament

    def test_lists_players_with_two_appearances(self):
        Player.objects.create(league=self.league, name='Newcomer')
        response = self.client.get(self.players_url)
        self.assertTrue(response.context['singles'])
        listed = {p.name: p.appearances for p in response.context['players']}
        self.assertEqual(listed, {'Abel': 3, 'Carl': 2, 'Jim': 2, 'Lupe': 3})
        self.assertNotContains(response, 'A Players')

    def test_entrants_are_seeded_by_match_record(self):
        self.enter('Lupe', 'Abel', 'Jim', 'Carl')
        response = self.client.get(self.players_url)
        entrants = response.context['entrants']
        self.assertEqual([p.name for p in entrants], ['Jim', 'Abel', 'Carl', 'Lupe'])
        self.assertEqual((entrants[0].total_wins, entrants[0].total_losses), (2, 0))
        self.assertEqual((entrants[3].total_wins, entrants[3].total_losses), (0, 2))
        self.assertContains(response, 'Create on ReadySetTourney')
        self.assertContains(response, 'One Pocket')

    def test_adds_only_this_leagues_players(self):
        other = make_league(name='Other', team_size=5, results_type=League.ResultsType.EIGHT_BALL)
        outsider = Player.objects.create(league=other, name='Outsider')
        response = self.client.post(self.players_url, {
            'player_ids': [self.players['Jim'].id, self.players['Abel'].id, outsider.id],
        })
        self.assertRedirects(response, self.players_url)
        self.assertEqual(
            set(TournamentPlayer.objects.values_list('player__name', flat=True)), {'Jim', 'Abel'},
        )

    def test_one_entrant_cannot_be_sent(self):
        self.enter('Jim')
        response = self.client.get(self.players_url)
        self.assertNotContains(response, 'Create on ReadySetTourney')
        response = self.client.post(self.send_url)
        self.assertRedirects(response, self.players_url)

    def test_bracket_page_sends_singles_back(self):
        self.enter('Jim', 'Abel')
        response = self.client.get(f"{reverse('tournament_bracket')}?league={self.league.pk}")
        self.assertRedirects(response, self.players_url)

    @patch('tournaments.views.urllib.request.urlopen')
    def test_sends_single_players_as_one_pocket(self, mock_urlopen):
        self.enter('Lupe', 'Jim', 'Abel')
        mock_urlopen.side_effect = [
            make_response({'token': 'abc123'}),
            make_response({'id': 7, 'url': 'http://readysettourney.example/t/7'}),
        ] + [make_response({}) for _ in range(8)]

        with self.settings(ONTHEHILL_USERNAME='svc', ONTHEHILL_PASSWORD='secret'):
            response = self.client.post(self.send_url, {'name': 'Bogies Singles', 'entry_fee': '20'})
        self.assertRedirects(response, self.players_url)
        payload = json.loads(mock_urlopen.call_args_list[1].args[0].data)
        self.assertEqual(payload['game_type'], 'one_pocket')
        self.assertEqual(payload['format'], 'double_elim')
        self.assertEqual(payload['teams'], [{'name': 'Jim'}, {'name': 'Abel'}, {'name': 'Lupe'}])
        self.assertEqual(payload['entry_fee'], '20')
