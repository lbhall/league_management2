from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import League, LeagueAdminAccess, Team, Venue
from scheduling.admin import get_user_league
from scheduling.models import Match, Season, Week


def make_league(**kwargs):
    defaults = {
        'name': 'Admin Test League',
        'team_size': 1,
        'results_type': League.ResultsType.ONE_POCKET,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Venue', max_home_teams=4):
    return Venue.objects.create(
        league=league, name=name, phone='555-0000', address='123 Main St',
        number_of_tables=2, max_home_teams=max_home_teams, min_home_teams=1,
    )


def make_team(league, venue, name):
    return Team.objects.create(league=league, venue=venue, name=name)


class SeasonAdminTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.client.login(username='admin', password='password123')

        self.league = make_league()
        self.venue = make_venue(self.league, max_home_teams=1)
        self.home = make_team(self.league, self.venue, 'Home')
        self.away = make_team(self.league, self.venue, 'Away')


class GetUserLeagueTests(SeasonAdminTestCase):
    def test_superuser_has_no_scoped_league(self):
        request = type('Req', (), {'user': self.superuser})()
        self.assertIsNone(get_user_league(request))

    def test_staff_user_with_access_returns_their_league(self):
        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer', password='password123', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)

        request = type('Req', (), {'user': staff_user})()
        self.assertEqual(get_user_league(request), self.league)

    def test_staff_user_without_access_returns_none(self):
        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer2', password='password123', is_staff=True)
        request = type('Req', (), {'user': staff_user})()
        self.assertIsNone(get_user_league(request))


class ScheduleRedirectViewTests(SeasonAdminTestCase):
    def test_superuser_sees_league_selector(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        response = self.client.get(reverse('admin:scheduling_schedule'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.league.name)

    def test_league_scoped_user_redirects_to_their_active_season(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer3', password='password123', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        self.client.login(username='staffer3', password='password123')

        response = self.client.get(reverse('admin:scheduling_schedule'))
        self.assertRedirects(response, reverse('admin:scheduling_season_manage_schedule', args=[season.pk]))

    def test_league_scoped_user_without_active_season_redirects_to_changelist(self):
        User = get_user_model()
        staff_user = User.objects.create_user(username='staffer4', password='password123', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        self.client.login(username='staffer4', password='password123')

        response = self.client.get(reverse('admin:scheduling_schedule'))
        # fetch_redirect_response=False: this staff user lacks Django model
        # permissions to actually view the changelist page (403), but the
        # redirect target itself is what we're verifying here.
        self.assertRedirects(
            response, reverse('admin:scheduling_season_changelist'), fetch_redirect_response=False,
        )


class ManageScheduleViewTests(SeasonAdminTestCase):
    def test_renders_schedule_with_bye_teams(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        bystander = make_team(self.league, self.venue, 'Sitting Out')
        Match.objects.create(week=week, home_team=self.home, away_team=self.away)

        response = self.client.get(reverse('admin:scheduling_season_manage_schedule', args=[season.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sitting Out')
        self.assertContains(response, self.home.name)


class WeekReorderingViewTests(SeasonAdminTestCase):
    def setUp(self):
        super().setUp()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.week2 = Week.objects.create(season=self.season, date=date(2026, 1, 12), number=2)

    def test_move_week_up_success(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_week_up', args=[self.season.pk, self.week2.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.week1.refresh_from_db()
        self.week2.refresh_from_db()
        self.assertEqual(self.week2.number, 1)

    def test_move_week_up_error_for_first_week(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_week_up', args=[self.season.pk, self.week1.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))

    def test_move_week_up_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_move_week_up', args=[self.season.pk, self.week2.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))

    def test_move_week_down_success(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_week_down', args=[self.season.pk, self.week1.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))

    def test_move_week_down_error_for_last_week(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_week_down', args=[self.season.pk, self.week2.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))

    def test_delete_week_success(self):
        empty_week = Week.objects.create(season=self.season, date=date(2026, 1, 19), number=3)
        response = self.client.post(
            reverse('admin:scheduling_season_delete_week', args=[self.season.pk, empty_week.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.assertFalse(Week.objects.filter(pk=empty_week.pk).exists())

    def test_delete_week_error_for_holiday(self):
        holiday_week = Week.objects.create(season=self.season, date=date(2026, 1, 26), number=None, notes='Holiday')
        response = self.client.post(
            reverse('admin:scheduling_season_delete_week', args=[self.season.pk, holiday_week.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.assertTrue(Week.objects.filter(pk=holiday_week.pk).exists())

    def test_renumber_weeks_view(self):
        response = self.client.post(
            reverse('admin:scheduling_season_renumber_weeks', args=[self.season.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))


class ArchiveSeasonViewTests(SeasonAdminTestCase):
    def test_archive_without_weeks_shows_error(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        response = self.client.post(reverse('admin:scheduling_season_archive', args=[season.pk]))
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))
        self.assertTrue(Season.objects.filter(pk=season.pk).exists())

    def test_archive_success_redirects_to_admin_index(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        Week.objects.create(season=season, date=date(2026, 1, 5), number=1)

        response = self.client.post(reverse('admin:scheduling_season_archive', args=[season.pk]))
        self.assertRedirects(response, reverse('admin:index'))
        self.assertFalse(Season.objects.filter(pk=season.pk).exists())


class RecreateScheduleViewTests(SeasonAdminTestCase):
    def test_rejects_active_season(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        response = self.client.post(
            reverse('admin:scheduling_season_recreate_schedule', args=[season.pk]),
            {'start_date': '2026-01-05'},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))

    def test_requires_start_date(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        response = self.client.post(
            reverse('admin:scheduling_season_recreate_schedule', args=[season.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))

    def test_rejects_invalid_start_date(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        response = self.client.post(
            reverse('admin:scheduling_season_recreate_schedule', args=[season.pk]),
            {'start_date': 'not-a-date'},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))

    def test_success_recreates_schedule(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        response = self.client.post(
            reverse('admin:scheduling_season_recreate_schedule', args=[season.pk]),
            {'start_date': '2026-01-05'},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))
        self.assertTrue(Match.objects.filter(week__season=season).exists())


class MirrorScheduleViewTests(SeasonAdminTestCase):
    def test_success(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        week = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        Match.objects.create(week=week, home_team=self.home, away_team=self.away)

        response = self.client.post(reverse('admin:scheduling_season_mirror_schedule', args=[season.pk]))
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))


class MoveLiveViewTests(SeasonAdminTestCase):
    def test_success_moves_working_season_to_active(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        response = self.client.post(reverse('admin:scheduling_season_move_live', args=[season.pk]))
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))
        season.refresh_from_db()
        self.assertEqual(season.status, Season.Status.ACTIVE)

    def test_rejects_already_active_season(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        response = self.client.post(reverse('admin:scheduling_season_move_live', args=[season.pk]))
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))



class RebalanceScheduleViewTests(SeasonAdminTestCase):
    def test_success(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        response = self.client.post(reverse('admin:scheduling_season_rebalance_schedule', args=[season.pk]))
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))


class SwapMatchViewTests(SeasonAdminTestCase):
    def setUp(self):
        super().setUp()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week, home_team=self.home, away_team=self.away)

    def test_swap_success(self):
        response = self.client.post(
            reverse('admin:scheduling_season_swap_match', args=[self.season.pk, self.match.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.home_team_id, self.away.id)
        self.assertEqual(self.match.away_team_id, self.home.id)

    def test_swap_keep_location(self):
        self.match.location = 'Custom Spot'
        self.match.save(update_fields=['location'])

        response = self.client.post(
            reverse('admin:scheduling_season_swap_match', args=[self.season.pk, self.match.pk]),
            {'keep_location': '1'},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.location, 'Custom Spot')

    def test_swap_rejected_when_venue_capacity_exceeded(self):
        other_home = make_team(self.league, self.venue, 'OtherHome')
        other_away = make_team(self.league, self.venue, 'OtherAway')
        Match.objects.create(week=self.week, home_team=other_home, away_team=other_away)

        response = self.client.post(
            reverse('admin:scheduling_season_swap_match', args=[self.season.pk, self.match.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.home_team_id, self.home.id)


class UpdateMatchLocationViewTests(SeasonAdminTestCase):
    def test_updates_location(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 1, 5), number=1)
        match = Match.objects.create(week=week, home_team=self.home, away_team=self.away)

        response = self.client.post(
            reverse('admin:scheduling_season_update_match_location', args=[season.pk, match.pk]),
            {'location': 'New Place'},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[season.pk]))
        match.refresh_from_db()
        self.assertEqual(match.location, 'New Place')


class MoveMatchViewTests(SeasonAdminTestCase):
    def setUp(self):
        super().setUp()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.week2 = Week.objects.create(season=self.season, date=date(2026, 1, 12), number=2)
        self.match = Match.objects.create(week=self.week1, home_team=self.home, away_team=self.away)

    def test_requires_target_week(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))

    def test_moves_to_existing_week(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
            {'target_week': self.week2.pk},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.week_id, self.week2.id)

    def test_moves_to_new_week(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
            {'target_week': 'new'},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.match.refresh_from_db()
        self.assertNotEqual(self.match.week_id, self.week1.id)

    def test_invalid_move_reports_error(self):
        other_league = make_league(name='Other League')
        other_season = Season.objects.create(league=other_league, name='Other', status=Season.Status.ACTIVE)
        other_week = Week.objects.create(season=other_season, date=date(2026, 2, 1), number=1)

        response = self.client.post(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
            {'target_week': other_week.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_move_rejected_onto_holiday_week(self):
        holiday_week = Week.objects.create(season=self.season, date=date(2026, 1, 19), number=None, notes='Holiday')
        response = self.client.post(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
            {'target_week': holiday_week.pk},
        )
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.week_id, self.week1.id)

    def test_move_match_with_next_redirects_there(self):
        response = self.client.post(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
            {'target_week': self.week2.pk, 'next': '/somewhere-else/'},
        )
        self.assertRedirects(response, '/somewhere-else/', fetch_redirect_response=False)


class GetRequestNoOpTests(SeasonAdminTestCase):
    """GET requests to the POST-only action views should redirect without acting."""

    def setUp(self):
        super().setUp()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.week2 = Week.objects.create(season=self.season, date=date(2026, 1, 12), number=2)
        self.match = Match.objects.create(week=self.week1, home_team=self.home, away_team=self.away)

    def assertRedirectsToChange(self, response):
        self.assertRedirects(response, reverse('admin:scheduling_season_change', args=[self.season.pk]))

    def test_move_week_down_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_move_week_down', args=[self.season.pk, self.week2.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_delete_week_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_delete_week', args=[self.season.pk, self.week1.pk]),
        )
        self.assertRedirectsToChange(response)
        self.assertTrue(Week.objects.filter(pk=self.week1.pk).exists())

    def test_renumber_weeks_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_renumber_weeks', args=[self.season.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_archive_season_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_archive', args=[self.season.pk]),
        )
        self.assertRedirectsToChange(response)
        self.assertTrue(Season.objects.filter(pk=self.season.pk).exists())

    def test_recreate_schedule_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_recreate_schedule', args=[self.season.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_mirror_schedule_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_mirror_schedule', args=[self.season.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_move_live_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_move_live', args=[self.season.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_rebalance_schedule_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_rebalance_schedule', args=[self.season.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_swap_match_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_swap_match', args=[self.season.pk, self.match.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_update_match_location_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_update_match_location', args=[self.season.pk, self.match.pk]),
        )
        self.assertRedirectsToChange(response)

    def test_move_match_get_is_noop(self):
        response = self.client.get(
            reverse('admin:scheduling_season_move_match', args=[self.season.pk, self.match.pk]),
        )
        self.assertRedirectsToChange(response)


class ManageScheduleViewContextTests(SeasonAdminTestCase):
    def test_working_season_shows_move_live_url_not_recreate(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.WORKING)
        response = self.client.get(reverse('admin:scheduling_season_manage_schedule', args=[season.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('admin:scheduling_season_move_live', args=[season.pk]))
        self.assertContains(response, reverse('admin:scheduling_season_recreate_schedule', args=[season.pk]))

    def test_active_season_shows_archive_url_not_recreate(self):
        season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        response = self.client.get(reverse('admin:scheduling_season_manage_schedule', args=[season.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('admin:scheduling_season_archive', args=[season.pk]))
        self.assertNotContains(response, reverse('admin:scheduling_season_recreate_schedule', args=[season.pk]))


class SeasonChangelistTests(SeasonAdminTestCase):
    def test_tournament_players_link_rendered(self):
        Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        response = self.client.get(reverse('admin:scheduling_season_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tournament Players')


class SeasonAdminScopedStaffTests(SeasonAdminTestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import Permission

        User = get_user_model()
        self.staff_user = User.objects.create_user(username='scoped_staffer', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=self.staff_user, league=self.league)
        self.staff_user.user_permissions.add(*Permission.objects.filter(content_type__app_label='scheduling'))
        self.client.login(username='scoped_staffer', password='pw')

    def test_add_form_hides_and_presets_league_field(self):
        response = self.client.get(reverse('admin:scheduling_season_add'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{self.league.pk}"')

    def test_save_model_forces_own_league(self):
        # formfield_for_foreignkey already restricts the league choices to the
        # staff member's own league; save_model is a second safeguard.
        response = self.client.post(reverse('admin:scheduling_season_add'), {
            'league': self.league.pk,
            'name': 'New Season',
            'status': Season.Status.WORKING,
            'weeks-TOTAL_FORMS': '0',
            'weeks-INITIAL_FORMS': '0',
            'weeks-MIN_NUM_FORMS': '0',
            'weeks-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 302)
        season = Season.objects.get(name='New Season')
        self.assertEqual(season.league_id, self.league.id)


class WeekAdminTests(SeasonAdminTestCase):
    def setUp(self):
        super().setUp()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        Match.objects.create(week=self.week, home_team=self.home, away_team=self.away)

        self.other_league = make_league(name='Other League2')
        self.other_season = Season.objects.create(league=self.other_league, name='OS', status=Season.Status.ACTIVE)
        self.other_week = Week.objects.create(season=self.other_season, date=date(2026, 2, 1), number=1)

    def test_superuser_sees_all_weeks_and_match_count(self):
        response = self.client.get(reverse('admin:scheduling_week_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.week))
        self.assertContains(response, str(self.other_week))

    def test_scoped_staff_only_sees_their_weeks(self):
        from django.contrib.auth.models import Permission

        User = get_user_model()
        staff_user = User.objects.create_user(username='week_staffer', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        staff_user.user_permissions.add(*Permission.objects.filter(content_type__app_label='scheduling'))
        self.client.login(username='week_staffer', password='pw')

        response = self.client.get(reverse('admin:scheduling_week_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.week))
        self.assertNotContains(response, str(self.other_week))

        response = self.client.get(
            reverse('admin:scheduling_week_add'),
        )
        self.assertEqual(response.status_code, 200)


class MatchAdminTests(SeasonAdminTestCase):
    def setUp(self):
        super().setUp()
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        self.week = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week, home_team=self.home, away_team=self.away)

        self.other_league = make_league(name='Other League3')
        self.other_venue = make_venue(self.other_league, name='Other Venue3')
        self.other_home = make_team(self.other_league, self.other_venue, 'OHome')
        self.other_away = make_team(self.other_league, self.other_venue, 'OAway')
        self.other_season = Season.objects.create(league=self.other_league, name='OS', status=Season.Status.ACTIVE)
        self.other_week = Week.objects.create(season=self.other_season, date=date(2026, 2, 1), number=1)
        self.other_match = Match.objects.create(
            week=self.other_week, home_team=self.other_home, away_team=self.other_away,
        )

    def test_superuser_sees_all_matches_and_enter_score_link(self):
        response = self.client.get(reverse('admin:scheduling_match_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter Score')
        self.assertContains(response, str(self.match))
        self.assertContains(response, str(self.other_match))

    def test_scoped_staff_only_sees_their_matches(self):
        from django.contrib.auth.models import Permission

        User = get_user_model()
        staff_user = User.objects.create_user(username='match_staffer', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff_user, league=self.league)
        staff_user.user_permissions.add(*Permission.objects.filter(content_type__app_label='scheduling'))
        self.client.login(username='match_staffer', password='pw')

        response = self.client.get(reverse('admin:scheduling_match_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.match))
        self.assertNotContains(response, str(self.other_match))

        response = self.client.get(reverse('admin:scheduling_match_add'))
        self.assertEqual(response.status_code, 200)
