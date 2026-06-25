from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from core.models import League, LeagueAdminAccess, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season, Week


def make_league(**kwargs):
    defaults = {
        'name': 'Results Admin League',
        'team_size': 2,
        'results_type': League.ResultsType.EIGHT_BALL,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Venue'):
    return Venue.objects.create(
        league=league, name=name, phone='555-0000', address='123 Main St',
        number_of_tables=2, max_home_teams=4, min_home_teams=1,
    )


class ResultsAdminTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.client.login(username='admin', password='password123')

        self.league = make_league()
        self.venue = make_venue(self.league)
        self.home_team = Team.objects.create(league=self.league, venue=self.venue, name='Home')
        self.away_team = Team.objects.create(league=self.league, venue=self.venue, name='Away')
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week, home_team=self.home_team, away_team=self.away_team)

    def make_scoped_staff(self, username='staffer', league=None):
        User = get_user_model()
        staff_user = User.objects.create_user(username=username, password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=league or self.league)
        staff_user.user_permissions.add(*Permission.objects.filter(content_type__app_label='results'))
        self.client.login(username=username, password='pw')
        return staff_user


class MatchResultAdminQuerysetTests(ResultsAdminTestCase):
    def test_superuser_sees_all(self):
        MatchResult.objects.create(match=self.match)
        response = self.client.get(reverse('admin:results_matchresult_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.match))

    def test_scoped_staff_only_sees_their_league(self):
        MatchResult.objects.create(match=self.match)

        other_league = make_league(name='Other League')
        other_venue = make_venue(other_league, name='Other Venue')
        other_home = Team.objects.create(league=other_league, venue=other_venue, name='OHome')
        other_away = Team.objects.create(league=other_league, venue=other_venue, name='OAway')
        other_season = Season.objects.create(league=other_league, name='OS', status=Season.Status.ACTIVE)
        other_week = Week.objects.create(season=other_season, date=date(2026, 2, 1), number=1)
        other_match = Match.objects.create(week=other_week, home_team=other_home, away_team=other_away)
        MatchResult.objects.create(match=other_match)

        self.make_scoped_staff()
        response = self.client.get(reverse('admin:results_matchresult_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.match))
        self.assertNotContains(response, str(other_match))


class CreatePlayerViewTests(ResultsAdminTestCase):
    def test_requires_post(self):
        response = self.client.get(
            reverse('admin:results_matchresult_create_player', args=[self.match.id]),
        )
        self.assertEqual(response.status_code, 405)

    def test_requires_name(self):
        response = self.client.post(
            reverse('admin:results_matchresult_create_player', args=[self.match.id]), {'name': ''},
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_duplicate_name(self):
        Player.objects.create(league=self.league, name='Alice')
        response = self.client.post(
            reverse('admin:results_matchresult_create_player', args=[self.match.id]), {'name': 'Alice'},
        )
        self.assertEqual(response.status_code, 400)

    def test_creates_player_successfully(self):
        response = self.client.post(
            reverse('admin:results_matchresult_create_player', args=[self.match.id]),
            {'name': 'Brand New', 'male': 'false'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['name'], 'Brand New')
        player = Player.objects.get(pk=data['id'])
        self.assertFalse(player.male)

    def test_404_for_match_outside_scoped_staff_league(self):
        other_league = make_league(name='Other League 2')
        other_venue = make_venue(other_league, name='Other Venue 2')
        other_home = Team.objects.create(league=other_league, venue=other_venue, name='OHome2')
        other_away = Team.objects.create(league=other_league, venue=other_venue, name='OAway2')
        other_season = Season.objects.create(league=other_league, name='OS2', status=Season.Status.ACTIVE)
        other_week = Week.objects.create(season=other_season, date=date(2026, 3, 1), number=1)
        other_match = Match.objects.create(week=other_week, home_team=other_home, away_team=other_away)

        self.make_scoped_staff()
        response = self.client.post(
            reverse('admin:results_matchresult_create_player', args=[other_match.id]), {'name': 'X'},
        )
        self.assertEqual(response.status_code, 404)


class EnterScoreEightBallViewTests(ResultsAdminTestCase):
    def setUp(self):
        super().setUp()
        self.home_p1 = Player.objects.create(league=self.league, name='HomeP1', team=self.home_team)
        self.home_p2 = Player.objects.create(league=self.league, name='HomeP2', team=self.home_team)
        self.away_p1 = Player.objects.create(league=self.league, name='AwayP1', team=self.away_team)
        self.away_p2 = Player.objects.create(league=self.league, name='AwayP2', team=self.away_team)

    def enter_score_url(self):
        return reverse('admin:results_matchresult_enter_score', args=[self.match.id])

    def post_score(self, home_wins, away_wins, **extra):
        data = {}
        for index, (player, wins) in enumerate(zip([self.home_p1, self.home_p2], home_wins)):
            data[f'home_player_{index}'] = player.id
            data[f'home_wins_{index}'] = wins
            data[f'home_runouts_{index}'] = 0
            data[f'home_eight_on_the_breaks_{index}'] = 0
        for index, (player, wins) in enumerate(zip([self.away_p1, self.away_p2], away_wins)):
            data[f'away_player_{index}'] = player.id
            data[f'away_wins_{index}'] = wins
            data[f'away_runouts_{index}'] = 0
            data[f'away_eight_on_the_breaks_{index}'] = 0
        data.update(extra)
        return self.client.post(self.enter_score_url(), data)

    def test_get_renders_empty_rows(self):
        response = self.client.get(self.enter_score_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'HomeP1')

    def test_post_success_saves_results_and_redirects(self):
        response = self.post_score([2, 0], [0, 2])
        self.assertEqual(response.status_code, 302)
        match_result = MatchResult.objects.get(match=self.match)
        self.assertEqual(
            PlayerMatchResult.objects.filter(match_result=match_result).count(), 4,
        )

    def test_post_with_next_url_redirects_there(self):
        response = self.post_score([2, 0], [0, 2], next='/somewhere/')
        self.assertRedirects(response, '/somewhere/', fetch_redirect_response=False)

    def test_post_rejects_duplicate_player_selection(self):
        data = {}
        for index in range(2):
            data[f'home_player_{index}'] = self.home_p1.id  # same player both slots
            data[f'home_wins_{index}'] = 1
            data[f'away_player_{index}'] = [self.away_p1.id, self.away_p2.id][index]
            data[f'away_wins_{index}'] = 1
        response = self.client.post(self.enter_score_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be selected in more than one slot')
        self.assertFalse(MatchResult.objects.filter(match=self.match).exclude(player_results=None).exists())

    def test_post_rejects_wrong_total_games(self):
        response = self.post_score([1, 0], [0, 1])  # total 2, expected team_size^2=4
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'must equal')

    # NOTE: a non-numeric value here currently crashes the view with an
    # uncaught ValueError (see spawned bug-fix task) rather than redisplaying
    # the form with a friendly error, so there is no passing test for that
    # path yet.


class EnterScoreOnePocketViewTests(ResultsAdminTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='One Pocket League', results_type=League.ResultsType.ONE_POCKET, team_size=1)
        self.venue = make_venue(self.league)
        self.home_team = Team.objects.create(league=self.league, venue=self.venue, name='Home')
        self.away_team = Team.objects.create(league=self.league, venue=self.venue, name='Away')
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week, home_team=self.home_team, away_team=self.away_team)

    def enter_score_url(self):
        return reverse('admin:results_matchresult_enter_score', args=[self.match.id])

    def test_get_renders_zeroed_scores(self):
        response = self.client.get(self.enter_score_url())
        self.assertEqual(response.status_code, 200)

    def test_post_success(self):
        response = self.client.post(self.enter_score_url(), {
            'home_team_score': '3', 'away_team_score': '1',
        })
        self.assertEqual(response.status_code, 302)
        match_result = MatchResult.objects.get(match=self.match)
        self.assertEqual(match_result.home_team_score, 3)
        self.assertEqual(match_result.away_team_score, 1)

    def test_post_with_next_url(self):
        response = self.client.post(self.enter_score_url(), {
            'home_team_score': '3', 'away_team_score': '1', 'next': '/elsewhere/',
        })
        self.assertRedirects(response, '/elsewhere/', fetch_redirect_response=False)

    def test_post_rejects_out_of_range_scores(self):
        response = self.client.post(self.enter_score_url(), {
            'home_team_score': '5', 'away_team_score': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'between 0 and 3')

    def test_post_rejects_no_winner(self):
        response = self.client.post(self.enter_score_url(), {
            'home_team_score': '2', 'away_team_score': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'winning score of 3')

    def test_post_rejects_two_winners(self):
        response = self.client.post(self.enter_score_url(), {
            'home_team_score': '3', 'away_team_score': '3',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Only one team')

    def test_post_rejects_non_numeric_scores(self):
        response = self.client.post(self.enter_score_url(), {
            'home_team_score': 'abc', 'away_team_score': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'valid numeric scores')


class EnterScoreDartsViewTests(ResultsAdminTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='Darts League', results_type=League.ResultsType.DARTS, team_size=2)
        self.venue = make_venue(self.league)
        self.home_team = Team.objects.create(league=self.league, venue=self.venue, name='Home')
        self.away_team = Team.objects.create(league=self.league, venue=self.venue, name='Away')
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week, home_team=self.home_team, away_team=self.away_team)

        self.home_p1 = Player.objects.create(league=self.league, name='HomeP1', team=self.home_team)
        self.home_p2 = Player.objects.create(league=self.league, name='HomeP2', team=self.home_team)
        self.away_p1 = Player.objects.create(league=self.league, name='AwayP1', team=self.away_team)
        self.away_p2 = Player.objects.create(league=self.league, name='AwayP2', team=self.away_team)

    def enter_score_url(self):
        return reverse('admin:results_matchresult_enter_score', args=[self.match.id])

    def post_score(self, **extra):
        data = {
            'home_team_score': '6', 'away_team_score': '3',
            'home_player_0': self.home_p1.id, 'home_player_1': self.home_p2.id,
            'home_hat_tricks_0': '1', 'home_three_in_a_beds_0': '0',
            'home_white_horses_0': '0', 'home_three_in_the_blacks_0': '0',
            'home_hat_tricks_1': '0', 'home_three_in_a_beds_1': '2',
            'home_white_horses_1': '0', 'home_three_in_the_blacks_1': '0',
            'away_player_0': self.away_p1.id, 'away_player_1': self.away_p2.id,
            'away_hat_tricks_0': '0', 'away_three_in_a_beds_0': '0',
            'away_white_horses_0': '1', 'away_three_in_the_blacks_0': '0',
            'away_hat_tricks_1': '0', 'away_three_in_a_beds_1': '0',
            'away_white_horses_1': '0', 'away_three_in_the_blacks_1': '0',
        }
        data.update(extra)
        return self.client.post(self.enter_score_url(), data)

    def test_get_renders_empty_rows(self):
        response = self.client.get(self.enter_score_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'HomeP1')
        self.assertContains(response, 'Games Won')

    def test_post_success_saves_team_score_and_player_stats(self):
        response = self.post_score()
        self.assertEqual(response.status_code, 302)

        match_result = MatchResult.objects.get(match=self.match)
        self.assertEqual(match_result.home_team_score, 6)
        self.assertEqual(match_result.away_team_score, 3)

        home_p1_result = PlayerMatchResult.objects.get(match_result=match_result, player=self.home_p1)
        self.assertEqual(home_p1_result.hat_tricks, 1)
        home_p2_result = PlayerMatchResult.objects.get(match_result=match_result, player=self.home_p2)
        self.assertEqual(home_p2_result.three_in_a_beds, 2)
        away_p1_result = PlayerMatchResult.objects.get(match_result=match_result, player=self.away_p1)
        self.assertEqual(away_p1_result.white_horses, 1)

    def test_post_with_next_url_redirects_there(self):
        response = self.post_score(next='/somewhere/')
        self.assertRedirects(response, '/somewhere/', fetch_redirect_response=False)

    def test_post_rejects_negative_team_score(self):
        response = self.post_score(home_team_score='-1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be negative')
        self.assertFalse(MatchResult.objects.filter(match=self.match).exclude(home_team_score=None).exists())

    def test_post_rejects_duplicate_player_selection(self):
        response = self.post_score(home_player_1=self.home_p1.id)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be selected in more than one slot')

    def test_post_rejects_non_numeric_team_score(self):
        response = self.post_score(home_team_score='not-a-number')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'valid numeric values')

    def test_post_rejects_non_numeric_player_stat(self):
        response = self.post_score(home_hat_tricks_0='not-a-number')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'valid numeric values')


class EnterScoreDispatchTests(ResultsAdminTestCase):
    def test_unsupported_results_type_redirects_with_error(self):
        League.objects.filter(pk=self.league.pk).update(results_type='something_else')
        response = self.client.get(
            reverse('admin:results_matchresult_enter_score', args=[self.match.id]),
        )
        self.assertRedirects(response, '/admin/', fetch_redirect_response=False)
