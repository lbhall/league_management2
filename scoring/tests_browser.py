"""End-to-end browser smoke tests for the scoring PWA.

These drive a real headless Chromium against a live server so we can exercise
the client-side JS (the "tap the winner" toggles), which the request-level
tests in tests.py can't reach. They skip cleanly when Playwright or its browser
binary isn't installed, so `manage.py test` still works everywhere; CI installs
Playwright in a dedicated "Browser tests" job.
"""
import os
import unittest
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone

from core.models import League, Player, Team
from scheduling.models import Match, Season, Week

from .models import LineupSlot, ScoringProfile
from .tests import make_venue

try:
    from playwright.sync_api import expect, sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


@unittest.skipUnless(_PLAYWRIGHT_AVAILABLE, 'playwright not installed')
class WinnerToggleBrowserTests(StaticLiveServerTestCase):
    """The games 'tap the winner' interaction is pure client-side JS. A prior
    regression (mousedown never firing on display:none radios) passed every
    request-level test but broke the actual gesture — this guards it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Playwright's sync API runs an event loop on this thread, which makes
        # Django flag the test's own ORM calls as "async-unsafe". They are in
        # fact safe (single-threaded test), so allow them for this class only.
        cls._prev_async_unsafe = os.environ.get('DJANGO_ALLOW_ASYNC_UNSAFE')
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
        try:
            cls._playwright = sync_playwright().start()
            cls.browser = cls._playwright.chromium.launch()
        except Exception as exc:  # browser binary missing, launch failure, etc.
            cls._restore_async_unsafe()
            super().tearDownClass()
            raise unittest.SkipTest(f'chromium unavailable: {exc}')

    @classmethod
    def _restore_async_unsafe(cls):
        if cls._prev_async_unsafe is None:
            os.environ.pop('DJANGO_ALLOW_ASYNC_UNSAFE', None)
        else:
            os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = cls._prev_async_unsafe

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._playwright.stop()
        cls._restore_async_unsafe()
        super().tearDownClass()

    def setUp(self):
        # Small EMC 8-ball match (team_size 2 -> a 2x2 game grid) with the
        # clear-winner behavior enabled and both lineups set.
        self.league = League.objects.create(
            name='EMC Fun Pool League',
            team_size=2,
            results_type=League.ResultsType.EIGHT_BALL,
            day_of_week=League.DayOfWeek.MONDAY,
            allow_game_winner_clear=True,
        )
        venue = make_venue(self.league)
        self.home_team = Team.objects.create(league=self.league, venue=venue, name='Sharks')
        self.away_team = Team.objects.create(league=self.league, venue=venue, name='Jets')
        home_players = [
            Player.objects.create(league=self.league, team=self.home_team, name=f'Home {i}')
            for i in range(1, 3)
        ]
        away_players = [
            Player.objects.create(league=self.league, team=self.away_team, name=f'Away {i}')
            for i in range(1, 3)
        ]
        season = Season.objects.create(
            league=self.league, name='S1', status=Season.Status.ACTIVE,
        )
        week = Week.objects.create(season=season, date=timezone.localdate(), number=1)
        self.match = Match.objects.create(
            week=week, home_team=self.home_team, away_team=self.away_team,
        )
        for pos in (1, 2):
            LineupSlot.objects.create(
                match=self.match, team=self.home_team, position=pos,
                player=home_players[pos - 1],
            )
            LineupSlot.objects.create(
                match=self.match, team=self.away_team, position=pos,
                player=away_players[pos - 1],
            )

        self.user = User.objects.create_user(
            username='admin@example.com', email='admin@example.com',
            password='pw12345!', is_staff=True, is_superuser=True,
        )
        ScoringProfile.objects.create(
            user=self.user, league=self.league,
            role=ScoringProfile.Role.ADMIN, is_approved=True,
        )

    def _new_logged_in_page(self):
        # Force a session cookie so we skip the login UI (not what we're testing).
        engine = import_module(settings.SESSION_ENGINE)
        session = engine.SessionStore()
        session[SESSION_KEY] = str(self.user.pk)
        session[BACKEND_SESSION_KEY] = 'django.contrib.auth.backends.ModelBackend'
        session[HASH_SESSION_KEY] = self.user.get_session_auth_hash()
        session.save()
        context = self.browser.new_context()
        context.add_cookies([{
            'name': settings.SESSION_COOKIE_NAME,
            'value': session.session_key,
            'url': self.live_server_url,
        }])
        return context, context.new_page()

    def test_tap_selected_winner_clears_it(self):
        context, page = self._new_logged_in_page()
        try:
            page.goto(f'{self.live_server_url}/score/match/{self.match.pk}/games/')

            radio = page.locator('input[name="winner_1_1"][value="home"]')
            label = page.locator('label:has(input[name="winner_1_1"][value="home"])')

            self.assertFalse(radio.is_checked())   # nothing recorded yet
            label.click()
            self.assertTrue(radio.is_checked())     # tap marks the winner
            label.click()
            self.assertFalse(radio.is_checked())    # tap again clears it (the regression)
        finally:
            context.close()

    def test_tap_switches_winner_between_players(self):
        context, page = self._new_logged_in_page()
        try:
            page.goto(f'{self.live_server_url}/score/match/{self.match.pk}/games/')

            home = page.locator('input[name="winner_1_1"][value="home"]')
            away = page.locator('input[name="winner_1_1"][value="away"]')
            page.locator('label:has(input[name="winner_1_1"][value="home"])').click()
            self.assertTrue(home.is_checked())
            page.locator('label:has(input[name="winner_1_1"][value="away"])').click()
            self.assertTrue(away.is_checked())
            self.assertFalse(home.is_checked())
        finally:
            context.close()

    def test_score_entry_survives_adding_a_player(self):
        # Regression: adding a sub mid-entry navigated away and back, wiping the
        # scores already entered. They must be preserved.
        context, page = self._new_logged_in_page()
        try:
            page.goto(f'{self.live_server_url}/score/match/{self.match.pk}/')

            played = page.locator('input[type="checkbox"][name^="played_"]').first
            wins = page.locator('select[name^="wins_"]').first
            played_name = played.get_attribute('name')
            wins_name = wins.get_attribute('name')

            played.check()
            wins.select_option(index=1)
            wins_value = wins.input_value()

            # Go add a new player for a sub in the middle of entering scores.
            page.locator('a.js-add-player').first.click()
            page.fill('input[name="name"]', 'Fresh Sub')
            page.locator('button[type="submit"]').click()
            page.wait_for_url(f'**/score/match/{self.match.pk}/')

            # The entries made before adding the player are still there.
            self.assertTrue(page.locator(f'input[name="{played_name}"]').is_checked())
            self.assertEqual(
                page.locator(f'select[name="{wins_name}"]').input_value(), wins_value,
            )
            # ...and the newly added player is now selectable.
            self.assertIn('Fresh Sub', page.content())
        finally:
            context.close()

    def test_eight_on_break_enabled_only_for_the_breaker(self):
        self.league.show_breaks = True
        self.league.save(update_fields=['show_breaks'])
        context, page = self._new_logged_in_page()
        try:
            page.goto(f'{self.live_server_url}/score/match/{self.match.pk}/games/')
            eb = page.locator('input[name="eb_1_1"]')

            # Round 1 -> home breaks. No winner picked yet, so it's disabled.
            expect(eb).to_be_disabled()
            page.locator('label:has(input[name="winner_1_1"][value="home"])').click()
            expect(eb).to_be_enabled()   # breaker won -> can mark 8-on-break
            page.locator('label:has(input[name="winner_1_1"][value="away"])').click()
            expect(eb).to_be_disabled()  # non-breaker won -> not allowed
        finally:
            context.close()
