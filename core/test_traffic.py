"""Page view counting and the staff traffic page."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.middleware import visitor_key
from core.models import League, LeagueAdminAccess, PageView
from core.test_views import ViewTestCase, make_league

BROWSER = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1'


class PageViewMiddlewareTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='Bogies East One Pocket League')

    def _get(self, path, **extra):
        extra.setdefault('HTTP_USER_AGENT', BROWSER)
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            return self.client.get(path, **extra)

    def test_html_page_load_is_recorded_with_league(self):
        self._get(reverse('home'))
        view = PageView.objects.get()
        self.assertEqual(view.path, '/')
        self.assertEqual(view.league, self.league)
        self.assertEqual(view.day, timezone.localdate())
        self.assertFalse(view.is_staff)
        self.assertEqual(len(view.visitor_key), 40)

    def test_same_visitor_same_day_shares_a_key(self):
        self._get(reverse('home'), REMOTE_ADDR='10.0.0.5')
        self._get(reverse('standings'), REMOTE_ADDR='10.0.0.5')
        keys = set(PageView.objects.values_list('visitor_key', flat=True))
        self.assertEqual(len(keys), 1)

    def test_different_device_gets_a_different_key(self):
        self._get(reverse('home'), REMOTE_ADDR='10.0.0.5')
        self._get(reverse('home'), REMOTE_ADDR='10.0.0.6')
        keys = set(PageView.objects.values_list('visitor_key', flat=True))
        self.assertEqual(len(keys), 2)

    def test_key_rotates_daily(self):
        class Req:
            META = {'REMOTE_ADDR': '10.0.0.5', 'HTTP_USER_AGENT': BROWSER}
        today = date(2026, 10, 22)
        self.assertNotEqual(
            visitor_key(Req(), today), visitor_key(Req(), today + timedelta(days=1)),
        )

    def test_forwarded_ip_is_used_behind_the_proxy(self):
        class Req:
            META = {'REMOTE_ADDR': '127.0.0.1', 'HTTP_X_FORWARDED_FOR': '203.0.113.9, 127.0.0.1',
                    'HTTP_USER_AGENT': BROWSER}

        class Direct:
            META = {'REMOTE_ADDR': '203.0.113.9', 'HTTP_USER_AGENT': BROWSER}
        today = date(2026, 10, 22)
        self.assertEqual(visitor_key(Req(), today), visitor_key(Direct(), today))

    def test_bots_admin_static_and_json_are_skipped(self):
        self._get(reverse('home'), HTTP_USER_AGENT='Googlebot/2.1 (+http://www.google.com/bot.html)')
        self._get(reverse('home'), HTTP_USER_AGENT='curl/8.0')
        self._get('/admin/login/')
        self._get(reverse('archived_standings_modal'))  # JSON (404 here, but never HTML)
        self._get(reverse('home'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(PageView.objects.count(), 0)

    def test_errors_and_non_get_are_skipped(self):
        self._get('/no-such-page/')
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            self.client.post(reverse('home'), HTTP_USER_AGENT=BROWSER)
        self.assertEqual(PageView.objects.count(), 0)

    def test_staff_visits_are_flagged(self):
        User = get_user_model()
        user = User.objects.create_user(username='op', password='pw', is_staff=True)
        self.client.force_login(user)
        self._get(reverse('home'))
        self.assertTrue(PageView.objects.get().is_staff)

    @override_settings(PAGEVIEW_TRACKING=False)
    def test_tracking_can_be_switched_off(self):
        self._get(reverse('home'))
        self.assertEqual(PageView.objects.count(), 0)


class TrafficPageTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='EMC Fun Pool League', results_type=League.ResultsType.EIGHT_BALL, team_size=5)
        self.other = make_league(name='Bogies', results_type=League.ResultsType.ONE_POCKET, team_size=1)
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        old = today - timedelta(days=45)
        rows = [
            # EMC: two visitors today (one twice), one yesterday, one too old
            (self.league, '/', 'aaa', today, False),
            (self.league, '/standings/', 'aaa', today, False),
            (self.league, '/', 'bbb', today, False),
            (self.league, '/schedule/', 'ccc', yesterday, False),
            (self.league, '/', 'ddd', old, False),
            # staff visit must not count
            (self.league, '/', 'eee', today, True),
            # other league
            (self.other, '/', 'fff', today, False),
        ]
        for league, path, key, day, is_staff in rows:
            PageView.objects.create(league=league, path=path, visitor_key=key, day=day, is_staff=is_staff)

    def _login_superuser(self):
        User = get_user_model()
        user = User.objects.create_superuser(username='root', password='pw', email='r@example.com')
        self.client.force_login(user)

    def test_anonymous_is_sent_to_admin_login(self):
        response = self.client.get(reverse('traffic'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_superuser_sees_all_leagues_and_can_filter(self):
        self._login_superuser()
        response = self.client.get(reverse('traffic'))
        self.assertEqual(response.status_code, 200)
        cards = {c['label']: c for c in response.context['cards']}
        # All leagues, today: 4 non-staff hits (3 EMC + 1 Bogies), 3 unique visitors.
        self.assertEqual(cards['Today']['hits'], 4)
        self.assertEqual(cards['Today']['uniques'], 3)
        self.assertEqual(cards['All time']['hits'], 6)

        response = self.client.get(reverse('traffic'), {'league': self.league.id})
        cards = {c['label']: c for c in response.context['cards']}
        self.assertEqual(cards['Today']['hits'], 3)
        self.assertEqual(cards['Today']['uniques'], 2)
        self.assertEqual(cards['Last 7 days']['hits'], 4)
        self.assertEqual(cards['Last 7 days']['uniques'], 3)
        self.assertEqual(cards['Last 30 days']['hits'], 4)
        self.assertEqual(cards['All time']['hits'], 5)

        top = {row['path']: row for row in response.context['top_pages']}
        self.assertEqual(top['/']['hits'], 2)
        self.assertEqual(top['/']['uniques'], 2)
        self.assertNotIn('/no-such/', top)

    def test_league_admin_only_sees_their_league(self):
        User = get_user_model()
        user = User.objects.create_user(username='opr', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=user, league=self.other)
        self.client.force_login(user)

        response = self.client.get(reverse('traffic'), {'league': self.league.id})
        self.assertEqual(response.status_code, 200)
        cards = {c['label']: c for c in response.context['cards']}
        # The EMC filter is ignored; only Bogies rows are counted.
        self.assertEqual(cards['All time']['hits'], 1)
        self.assertEqual(len(response.context['league_options']), 1)

    def test_nav_link_only_for_staff(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
            self.assertNotContains(response, 'href="/traffic/"')
            self._login_superuser()
            response = self.client.get(reverse('home'))
            self.assertContains(response, 'href="/traffic/"')


class PrunePageviewsTests(TestCase):
    def test_prune_deletes_only_old_rows(self):
        from django.core.management import call_command
        today = timezone.localdate()
        PageView.objects.create(path='/', visitor_key='a', day=today)
        PageView.objects.create(path='/', visitor_key='b', day=today - timedelta(days=500))
        call_command('prune_pageviews', days=400)
        self.assertEqual(PageView.objects.count(), 1)
