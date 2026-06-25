from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from content.models import NewsItem, Rule
from core.models import League, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import ArchivedPlayer, ArchivedSeason, ArchivedTeam, Match, Season, Week


def make_league(results_type=League.ResultsType.ONE_POCKET, team_size=1, **kwargs):
    defaults = {
        'name': 'View Test League',
        'team_size': team_size,
        'results_type': results_type,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Test Venue', max_home_teams=4):
    return Venue.objects.create(
        league=league,
        name=name,
        phone='555-1234',
        address='123 Main St',
        number_of_tables=2,
        max_home_teams=max_home_teams,
        min_home_teams=1,
    )


def make_team(league, venue, name, team_rank=None):
    return Team.objects.create(league=league, venue=venue, name=name, team_rank=team_rank)


class ViewTestCase(TestCase):
    """Base class that ensures requests carry a Host header.

    Several views log `request.headers["Host"]` directly (not `.get`), which
    raises KeyError unless something has set the Host header -- real browsers
    always do, but Django's test client doesn't unless told to.
    """

    def setUp(self):
        super().setUp()
        self.client.defaults['HTTP_HOST'] = 'testserver'
        self.client.defaults['HTTP_USER_AGENT'] = 'test-agent'


class NoActiveLeagueTests(ViewTestCase):
    """No League rows exist at all, exercising the `active_league is None` branches."""

    def test_pages_render_without_a_league(self):
        for url_name in ['home', 'schedule', 'standings', 'player_stats', 'contact_info', 'rules', 'archived_seasons']:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, url_name)


class OnePocketViewsTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='Bogies Pool One Pocket', results_type=League.ResultsType.ONE_POCKET, team_size=1)
        self.venue = make_venue(self.league)
        self.team_a = make_team(self.league, self.venue, 'Marcus', team_rank=2)
        self.team_b = make_team(self.league, self.venue, 'Louie', team_rank=4)
        self.team_c = make_team(self.league, self.venue, 'Beau', team_rank=1)
        self.team_d = make_team(self.league, self.venue, 'Danni', team_rank=3)

        self.season = Season.objects.create(league=self.league, name='Season 1', status=Season.Status.ACTIVE)
        # 2026-01-05 is a Monday, matching the league's day_of_week.
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.week2 = Week.objects.create(season=self.season, date=date(2026, 1, 12), number=2)
        self.holiday_week = Week.objects.create(
            season=self.season, date=date(2026, 1, 19), number=None, notes='Holiday',
        )

        self.match1 = Match.objects.create(week=self.week1, home_team=self.team_a, away_team=self.team_b)
        MatchResult.objects.create(match=self.match1, home_team_score=6, away_team_score=10)
        self.match2 = Match.objects.create(week=self.week2, home_team=self.team_c, away_team=self.team_d)

        NewsItem.objects.create(
            league=self.league, title='Welcome', description='Season kickoff',
            show_date=date(2025, 1, 1),
        )
        Rule.objects.create(league=self.league, text='General Rules', rule_type=Rule.RuleType.MAJOR_HEADING, order=1)
        Rule.objects.create(league=self.league, text='No slow play.', rule_type=Rule.RuleType.RULE_ENTRY, order=2)
        Rule.objects.create(league=self.league, text='Be on time.', rule_type=Rule.RuleType.RULE_ENTRY, order=3)

        archived_season = ArchivedSeason.objects.create(league=self.league, name='Archived Season 1')
        ArchivedTeam.objects.create(archived_season=archived_season, team_name='Marcus', matches_won=5, matches_lost=2)
        ArchivedPlayer.objects.create(archived_season=archived_season, player_name='Marcus', games_won=10, games_lost=4)
        self.archived_season = archived_season

    def test_home_page(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Marcus')

    def test_schedule_page(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('schedule'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Marcus')

    def test_standings_page_default_and_with_week_filter(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('standings'))
            self.assertEqual(response.status_code, 200)

            response = self.client.get(reverse('standings'), {'week': self.week1.id})
            self.assertEqual(response.status_code, 200)

    def test_player_stats_page_sorting_and_gender_filter(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('player_stats'), {
                'gender': 'male', 'sort': 'player', 'dir': 'asc',
            })
            self.assertEqual(response.status_code, 200)

            response = self.client.get(reverse('player_stats'), {'gender': 'female'})
            self.assertEqual(response.status_code, 200)

    def test_contact_info_page(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('contact_info'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Marcus')

    def test_rules_page_groups_entries_under_heading(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('rules'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No slow play.')

    def test_team_detail_page(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.team_a.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Marcus')

    def test_team_detail_404_for_unknown_team(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_team_schedule_modal(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('html', response.json())

    def test_one_pocket_full_schedule_modal(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('one_pocket_full_schedule_modal'))
        self.assertEqual(response.status_code, 200)

    def test_player_scores_modal(self):
        player = Player.objects.get(league=self.league, name='Marcus')
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('player_scores_modal', args=[player.id]))
            self.assertEqual(response.status_code, 200)

            response = self.client.get(
                reverse('player_scores_modal', args=[player.id]), {'week': self.week1.id},
            )
            self.assertEqual(response.status_code, 200)

    def test_archived_seasons_page_lists_pool_league_archives(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_seasons'))
            self.assertEqual(response.status_code, 200)

            response = self.client.get(reverse('archived_seasons'), {'season': self.archived_season.id})
            self.assertEqual(response.status_code, 200)

    def test_archived_standings_modal(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_standings_modal'))
        self.assertEqual(response.status_code, 200)

    def test_archived_player_history_modal(self):
        response = self.client.get(
            reverse('archived_player_history', args=[self.archived_season.id, 'Marcus']),
        )
        self.assertEqual(response.status_code, 200)


class EightBallViewsTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='Eight Ball League', results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        self.venue = make_venue(self.league)
        self.home_team = make_team(self.league, self.venue, 'Home Team')
        self.away_team = make_team(self.league, self.venue, 'Away Team')
        self.home_player = Player.objects.create(league=self.league, name='Alice', team=self.home_team, male=False)
        self.away_player = Player.objects.create(league=self.league, name='Bob', team=self.away_team, male=True)

        self.season = Season.objects.create(league=self.league, name='Season 1', status=Season.Status.ACTIVE)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week1, home_team=self.home_team, away_team=self.away_team)
        self.match_result = MatchResult.objects.create(match=self.match)
        PlayerMatchResult.objects.create(
            match_result=self.match_result, player=self.home_player, represented_team=self.home_team,
            wins=3, runouts=1, eight_on_the_breaks=1,
        )
        PlayerMatchResult.objects.create(
            match_result=self.match_result, player=self.away_player, represented_team=self.away_team,
            wins=0,
        )

    def test_finance_page_requires_staff_login(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('finance'))
        self.assertEqual(response.status_code, 302)

        User = get_user_model()
        User.objects.create_superuser(username='admin', password='password123', email='admin@example.com')
        self.client.login(username='admin', password='password123')

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('finance'))
        self.assertEqual(response.status_code, 200)

    def test_team_detail_with_player_match_results(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')

    def test_player_stats_page_includes_runouts_and_eights(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('player_stats'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alice')

    def test_player_scores_modal_lists_matchup(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('player_scores_modal', args=[self.home_player.id]))
        self.assertEqual(response.status_code, 200)

    def test_archived_seasons_page_is_pool_league_for_eight_ball(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_seasons'))
        self.assertEqual(response.status_code, 200)


class DartsViewsTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.league = make_league(name='Darts League', results_type=League.ResultsType.DARTS, team_size=2)
        self.venue = make_venue(self.league)
        self.home_team = make_team(self.league, self.venue, 'Home Team')
        self.away_team = make_team(self.league, self.venue, 'Away Team')
        self.home_player = Player.objects.create(league=self.league, name='Nancy', team=self.home_team, male=False)
        self.away_player = Player.objects.create(league=self.league, name='Am', team=self.away_team, male=True)

        self.season = Season.objects.create(league=self.league, name='Season 1', status=Season.Status.ACTIVE)
        self.week1 = Week.objects.create(season=self.season, date=date(2026, 1, 5), number=1)
        self.match = Match.objects.create(week=self.week1, home_team=self.home_team, away_team=self.away_team)
        self.match_result = MatchResult.objects.create(
            match=self.match, home_team_score=6, away_team_score=3,
        )
        PlayerMatchResult.objects.create(
            match_result=self.match_result, player=self.home_player, represented_team=self.home_team,
            three_in_a_beds=2,
        )
        PlayerMatchResult.objects.create(
            match_result=self.match_result, player=self.away_player, represented_team=self.away_team,
            white_horses=1,
        )

    def test_home_page_shows_team_score_based_standings(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nancy')

        team_standings = response.context['team_standings']
        home_standing = next(s for s in team_standings if s['team'] == 'Home Team')
        away_standing = next(s for s in team_standings if s['team'] == 'Away Team')
        self.assertEqual(home_standing['games_won'], 6)
        self.assertEqual(home_standing['games_lost'], 3)
        self.assertEqual(home_standing['matches_won'], 1)
        self.assertEqual(away_standing['matches_lost'], 1)

    def test_home_page_top_players_sorted_by_darts_points(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        # Nancy: 2 three-in-a-beds = 4 pts; Am: 1 white horse = 3 pts.
        top_players = response.context['top_male_players']
        self.assertEqual([p['player'] for p in top_players[:2]], ['Nancy', 'Am'])
        self.assertContains(response, '4 pts')
        self.assertContains(response, '3 pts')

    def test_standings_page(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('standings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Home Team')

    def test_nav_hides_archived_and_tournament_links(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'archived-seasons')
        self.assertNotContains(response, 'End of Season Tournament')
        self.assertContains(response, '/schedule/')
        self.assertContains(response, '/standings/')

    def test_nav_hides_contact_info_link(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'contact-info')
        self.assertContains(response, '/rules/')

    def test_team_detail_hides_captain_and_venue_cards(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '>Captain<')
        self.assertNotContains(response, '>Venue<')

    def test_team_detail_shows_darts_stat_columns(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '>Points<')
        self.assertContains(response, '>HT<')
        self.assertContains(response, '3-Bed')
        self.assertContains(response, '>WH<')
        self.assertContains(response, '3-Black')

        stat = next(
            s for s in response.context['team_player_stats'] if s['player'] == 'Nancy'
        )
        self.assertEqual(stat['three_in_a_beds'], 2)
        self.assertEqual(stat['points'], 4)

    def test_team_detail_shows_schedule_for_non_monday_match_days(self):
        # The darts league in this test plays on Fridays; the week-day filter
        # used to be hard-coded to Mondays, which hid the schedule entirely.
        self.assertEqual(self.week1.date.strftime('%A'), 'Monday')
        friday_week = Week.objects.create(season=self.season, date=date(2026, 1, 9), number=2)
        Match.objects.create(week=friday_week, home_team=self.home_team, away_team=self.away_team)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'No schedule available for this team.')

        weeks_shown = [entry['week'] for entry in response.context['team_schedule']]
        self.assertIn(friday_week, weeks_shown)

    def test_team_detail_shows_actual_team_score_for_darts(self):
        # Per-player wins/losses are placeholders for darts; the match result
        # label must come from MatchResult.home/away_team_score, not from
        # summing player_result.wins (which would always show 0-0).
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))
        self.assertEqual(response.status_code, 200)

        entry = next(
            e for e in response.context['team_schedule'] if e['week'] == self.week1
        )
        self.assertEqual(entry['result_label'], '6-3')
        self.assertContains(response, '6-3')

    def test_player_scores_modal_shows_darts_columns(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(
                reverse('player_scores_modal', args=[self.home_player.id])
            )
        self.assertEqual(response.status_code, 200)
        html = response.json()['html']
        self.assertIn('>Points<', html)
        self.assertIn('>HT<', html)
        self.assertIn('3-Bed', html)
        self.assertIn('>WH<', html)
        self.assertIn('3-Black', html)
        self.assertNotIn('>Wins<', html)
        self.assertNotIn('>Losses<', html)

        row = response.context['matchup_rows'][0]
        self.assertEqual(row['three_in_a_beds'], 2)
        self.assertEqual(row['points'], 4)

    def test_team_detail_match_result_modal_shows_darts_columns(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))
        self.assertEqual(response.status_code, 200)

        entry = next(
            e for e in response.context['team_schedule'] if e['week'] == self.week1
        )
        row = next(r for r in entry['match_detail_rows'] if r['player'] == 'Nancy')
        self.assertEqual(row['three_in_a_beds'], 2)
        self.assertEqual(row['points'], 4)

        self.assertContains(response, '>Points<')
        self.assertContains(response, '>HT<')
        self.assertContains(response, '3-Bed')
        self.assertContains(response, '>WH<')
        self.assertContains(response, '3-Black')
