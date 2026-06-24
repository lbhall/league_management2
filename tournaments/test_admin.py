from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import League, LeagueAdminAccess, Player
from scheduling.models import Season
from tournaments.admin import get_user_league
from tournaments.models import Tournament, TournamentPlayer


def make_league(**kwargs):
    defaults = {
        'name': 'Tournament Admin League',
        'team_size': 1,
        'results_type': League.ResultsType.ONE_POCKET,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


class TournamentAdminTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.league = make_league()
        self.other_league = make_league(name='Other League')
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.tournament = Tournament.objects.create(season=self.season)


class GetUserLeagueTests(TournamentAdminTestCase):
    def test_superuser_returns_none(self):
        request = type('Req', (), {'user': self.superuser})()
        self.assertIsNone(get_user_league(request))

    def test_staff_with_access_returns_league(self):
        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        request = type('Req', (), {'user': staff_user})()
        self.assertEqual(get_user_league(request), self.league)


class TournamentAdminPermissionTests(TournamentAdminTestCase):
    def test_superuser_can_view_changelist(self):
        self.client.login(username='admin', password='password123')
        response = self.client.get(reverse('admin:tournaments_tournament_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Manage Tournament Players')

    def test_non_staff_cannot_access_module(self):
        User = get_user_model()
        non_staff = User.objects.create_user(username='regular', password='pw', is_staff=False)
        self.client.login(username='regular', password='pw')
        response = self.client.get(reverse('admin:tournaments_tournament_changelist'))
        self.assertEqual(response.status_code, 302)  # redirected to login

    def test_league_scoped_staff_only_sees_their_league_tournaments(self):
        other_season = Season.objects.create(league=self.other_league, name='S2', status=Season.Status.ACTIVE)
        other_tournament = Tournament.objects.create(season=other_season)

        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer2', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        self.client.login(username='staffer2', password='pw')

        response = self.client.get(reverse('admin:tournaments_tournament_changelist'))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse('admin:tournaments_tournament_change', args=[other_tournament.pk]))
        # Filtered out of get_queryset entirely, so admin redirects rather than 403ing.
        self.assertEqual(response.status_code, 302)

        response = self.client.get(reverse('admin:tournaments_tournament_change', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_staff_without_league_access_can_still_view(self):
        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer3', password='pw', is_staff=True)
        self.client.login(username='staffer3', password='pw')

        response = self.client.get(reverse('admin:tournaments_tournament_change', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)


class TournamentPlayerAdminPermissionTests(TournamentAdminTestCase):
    def setUp(self):
        super().setUp()
        self.player = Player.objects.create(league=self.league, name='Alice')
        self.tp = TournamentPlayer.objects.create(tournament=self.tournament, player=self.player)

    def test_superuser_sees_changelist(self):
        self.client.login(username='admin', password='password123')
        response = self.client.get(reverse('admin:tournaments_tournamentplayer_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tournament Management Page')

    def test_league_scoped_staff_queryset_filtered(self):
        other_season = Season.objects.create(league=self.other_league, name='S2', status=Season.Status.ACTIVE)
        other_tournament = Tournament.objects.create(season=other_season)
        other_player = Player.objects.create(league=self.other_league, name='Bob')
        other_tp = TournamentPlayer.objects.create(tournament=other_tournament, player=other_player)

        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer4', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        self.client.login(username='staffer4', password='pw')

        response = self.client.get(reverse('admin:tournaments_tournamentplayer_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')
        self.assertNotContains(response, 'Bob')

        response = self.client.get(reverse('admin:tournaments_tournamentplayer_change', args=[other_tp.pk]))
        # Filtered out of get_queryset entirely, so admin redirects rather than 403ing.
        self.assertEqual(response.status_code, 302)
