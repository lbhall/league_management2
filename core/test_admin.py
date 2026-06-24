from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.admin import get_user_league
from core.models import League, LeagueAdminAccess, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season, Week


def make_league(**kwargs):
    defaults = {
        'name': 'Core Admin League',
        'team_size': 3,
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


class CoreAdminTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.league = make_league()
        self.other_league = make_league(name='Other League')
        self.venue = make_venue(self.league)
        self.other_venue = make_venue(self.other_league, name='Other Venue')
        self.team = Team.objects.create(league=self.league, venue=self.venue, name='Team A')
        self.other_team = Team.objects.create(league=self.other_league, venue=self.other_venue, name='Team B')

    def login_as_superuser(self):
        self.client.login(username='admin', password='password123')

    def make_scoped_staff(self, username='staffer', league=None):
        from django.contrib.auth.models import Permission

        User = get_user_model()
        staff_user = User.objects.create_user(username=username, password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=league or self.league)
        # LeagueScopedAdminMixin only narrows the queryset; Django's own model
        # permissions still gate add/change/view on Venue/Team/Player admins.
        staff_user.user_permissions.add(*Permission.objects.filter(content_type__app_label='core'))
        self.client.login(username=username, password='pw')
        return staff_user


class GetUserLeagueTests(CoreAdminTestCase):
    def test_superuser_has_no_scoped_league(self):
        request = type('Req', (), {'user': self.superuser})()
        self.assertIsNone(get_user_league(request))

    def test_scoped_staff_returns_their_league(self):
        staff_user = self.make_scoped_staff()
        request = type('Req', (), {'user': staff_user})()
        self.assertEqual(get_user_league(request), self.league)


class LeagueAdminPermissionTests(CoreAdminTestCase):
    def test_superuser_full_access(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_league_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Financial Breakdown')
        self.assertContains(response, 'Tournament Players')

        response = self.client.get(reverse('admin:core_league_add'))
        self.assertEqual(response.status_code, 200)

    def test_scoped_staff_sees_only_their_league(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_league_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.league.name)
        self.assertNotContains(response, self.other_league.name)

    def test_scoped_staff_cannot_add_league(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_league_add'))
        self.assertEqual(response.status_code, 403)

    def test_scoped_staff_cannot_view_other_league(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_league_change', args=[self.other_league.pk]))
        # get_queryset() already filters it out, so admin redirects rather than 403ing.
        self.assertEqual(response.status_code, 302)

    def test_scoped_staff_can_view_own_league(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_league_change', args=[self.league.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'financial-breakdown')

    def test_staff_with_no_access_at_all_gets_no_module_permission(self):
        User = get_user_model()
        User.objects.create_user(username='plain_staff', password='pw', is_staff=True)
        self.client.login(username='plain_staff', password='pw')
        response = self.client.get(reverse('admin:core_league_changelist'))
        self.assertEqual(response.status_code, 403)  # has_view_permission() returns False


class FinancialBreakdownViewTests(CoreAdminTestCase):
    def test_renders_without_active_season(self):
        self.login_as_superuser()
        response = self.client.get(
            reverse('admin:core_league_financial_breakdown', args=[self.league.pk]),
        )
        self.assertEqual(response.status_code, 200)

    def test_renders_with_active_season_and_results(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        away_team = Team.objects.create(league=self.league, venue=self.venue, name='Away Team')
        match = Match.objects.create(week=week, home_team=self.team, away_team=away_team)
        match_result = MatchResult.objects.create(match=match)
        player = Player.objects.create(league=self.league, name='Alice', team=self.team, male=False)
        PlayerMatchResult.objects.create(
            match_result=match_result, player=player, represented_team=self.team,
            wins=3, runouts=1, eight_on_the_breaks=1,
        )

        self.login_as_superuser()
        response = self.client.get(
            reverse('admin:core_league_financial_breakdown', args=[self.league.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')


class VenueAdminTests(CoreAdminTestCase):
    def test_superuser_list_filter_includes_league(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_venue_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.venue.name)
        self.assertContains(response, self.other_venue.name)

    def test_scoped_staff_only_sees_their_venues(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_venue_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.venue.name)
        self.assertNotContains(response, self.other_venue.name)

    def test_scoped_staff_add_venue_auto_assigns_league(self):
        self.make_scoped_staff()
        # formfield_for_foreignkey already restricts the league choices to the
        # staff member's own league; save_model is a second safeguard.
        response = self.client.post(reverse('admin:core_venue_add'), {
            'league': self.league.id,
            'name': 'New Venue',
            'phone': '555-9999',
            'address': '456 Other St',
            'number_of_tables': 3,
            'max_home_teams': 2,
            'min_home_teams': 1,
        })
        self.assertEqual(response.status_code, 302)
        new_venue = Venue.objects.get(name='New Venue')
        self.assertEqual(new_venue.league_id, self.league.id)


class TeamAdminTests(CoreAdminTestCase):
    def test_superuser_changelist(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_team_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.team.name)
        self.assertContains(response, self.other_team.name)

    def test_scoped_staff_only_sees_their_teams(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_team_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.team.name)
        self.assertNotContains(response, self.other_team.name)

    def test_league_options_view_returns_venues_and_captains(self):
        player = Player.objects.create(league=self.league, name='Cap', team=self.team)
        self.login_as_superuser()
        response = self.client.get(
            reverse('admin:core_team_league_options'),
            {'league_id': self.league.id, 'team_id': self.team.id},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn({'id': self.venue.id, 'name': self.venue.name}, data['venues'])
        self.assertIn({'id': player.id, 'name': 'Cap'}, data['captains'])

    def test_league_options_view_scoped_staff_ignores_requested_league_id(self):
        self.make_scoped_staff()
        response = self.client.get(
            reverse('admin:core_team_league_options'),
            {'league_id': self.other_league.id},
        )
        data = response.json()
        venue_ids = {v['id'] for v in data['venues']}
        self.assertIn(self.venue.id, venue_ids)
        self.assertNotIn(self.other_venue.id, venue_ids)

    def test_league_options_view_without_league_id_returns_empty(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_team_league_options'))
        data = response.json()
        self.assertEqual(data['venues'], [])
        self.assertEqual(data['captains'], [])

    def test_add_team_with_inline_player_creates_player(self):
        self.login_as_superuser()
        response = self.client.post(reverse('admin:core_team_add'), {
            'league': self.league.id,
            'venue': self.venue.id,
            'name': 'Brand New Team',
            'players-TOTAL_FORMS': '1',
            'players-INITIAL_FORMS': '0',
            'players-MIN_NUM_FORMS': '0',
            'players-MAX_NUM_FORMS': '1000',
            'players-0-name': 'New Player',
            'players-0-phone': '',
            'players-0-male': 'on',
            'players-0-league': self.league.id,
        })
        self.assertEqual(response.status_code, 302)
        team = Team.objects.get(name='Brand New Team')
        self.assertTrue(Player.objects.filter(name='New Player', team=team, league=self.league).exists())


class PlayerAdminTests(CoreAdminTestCase):
    def setUp(self):
        super().setUp()
        self.player = Player.objects.create(league=self.league, name='Alice', team=self.team)
        self.other_player = Player.objects.create(league=self.other_league, name='Bob', team=self.other_team)

    def test_superuser_changelist(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_player_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')
        self.assertContains(response, 'Bob')

    def test_scoped_staff_only_sees_their_players(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_player_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')
        self.assertNotContains(response, 'Bob')

    def test_league_teams_view_returns_teams_for_league(self):
        self.login_as_superuser()
        response = self.client.get(
            reverse('admin:core_player_league_teams'), {'league_id': self.league.id},
        )
        data = response.json()
        self.assertIn({'id': self.team.id, 'name': self.team.name}, data['teams'])

    def test_league_teams_view_scoped_staff_ignores_requested_league_id(self):
        self.make_scoped_staff()
        response = self.client.get(
            reverse('admin:core_player_league_teams'), {'league_id': self.other_league.id},
        )
        data = response.json()
        team_ids = {t['id'] for t in data['teams']}
        self.assertIn(self.team.id, team_ids)
        self.assertNotIn(self.other_team.id, team_ids)

    def test_league_teams_view_without_league_id(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_player_league_teams'))
        self.assertEqual(response.json()['teams'], [])


class LeagueScopedFilterTests(CoreAdminTestCase):
    def test_venue_filter_scopes_lookups_for_staff(self):
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_team_changelist'), {'venue': self.venue.id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.team.name)

    def test_team_filter_scopes_lookups_for_staff(self):
        Player.objects.create(league=self.league, name='Alice', team=self.team)
        self.make_scoped_staff()
        response = self.client.get(reverse('admin:core_player_changelist'), {'team': self.team.id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')

    def test_filters_render_for_superuser(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_team_changelist'))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse('admin:core_player_changelist'))
        self.assertEqual(response.status_code, 200)


class FailedLoginAdminTests(CoreAdminTestCase):
    def test_cannot_add_or_change(self):
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_failedlogin_add'))
        self.assertEqual(response.status_code, 403)

    def test_changelist_renders(self):
        from core.models import FailedLogin
        FailedLogin.objects.create(username='baduser', ip_address='1.2.3.4')
        self.login_as_superuser()
        response = self.client.get(reverse('admin:core_failedlogin_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'baduser')
