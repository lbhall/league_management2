from datetime import date, datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from content.models import NewsItem, Rule
from core.models import League, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import (
    ArchivedMatch,
    ArchivedPlayer,
    ArchivedPlayerMatchResult,
    ArchivedSeason,
    ArchivedTeam,
    Match,
    Season,
    Week,
)


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
        self.league = make_league(name='Bogies East One Pocket League', results_type=League.ResultsType.ONE_POCKET, team_size=1)
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

        archived_season = ArchivedSeason.objects.create(
            league=self.league, name='Archived Season 1', results_type=League.ResultsType.ONE_POCKET, team_size=1,
        )
        ArchivedTeam.objects.create(
            archived_season=archived_season, team_name='Marcus', matches_won=5, matches_lost=2,
            league_rank=1, team_rank=2,
        )
        ArchivedTeam.objects.create(
            archived_season=archived_season, team_name='Louie', matches_won=2, matches_lost=5,
            league_rank=2, team_rank=4,
        )
        ArchivedPlayer.objects.create(archived_season=archived_season, player_name='Marcus', games_won=10, games_lost=4)
        archived_match = ArchivedMatch.objects.create(
            archived_season=archived_season, date=date(2025, 6, 2), week_number=1,
            home_team_name='Marcus', away_team_name='Louie', home_team_score=6, away_team_score=10,
            home_team_rank=2, away_team_rank=4, home_race=6, away_race=10, completed=True,
            result_recorded_at=datetime(2025, 6, 9, 20, 30, tzinfo=dt_timezone.utc),
        )
        ArchivedPlayerMatchResult.objects.create(
            archived_match=archived_match, player_name='Marcus', team_name='Marcus', wins=6, losses=10,
        )
        ArchivedPlayerMatchResult.objects.create(
            archived_match=archived_match, player_name='Louie', team_name='Louie', wins=10, losses=6,
        )
        self.archived_season = archived_season

        # Another league's archive must never leak into this site.
        self.other_league = make_league(name='Other Pool League', results_type=League.ResultsType.EIGHT_BALL, team_size=5)
        self.other_archived_season = ArchivedSeason.objects.create(
            league=self.other_league, name='Other Archive', results_type=League.ResultsType.EIGHT_BALL, team_size=5,
        )
        ArchivedTeam.objects.create(archived_season=self.other_archived_season, team_name='Other Team')

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

    def test_team_schedule_modal_tags_wins_and_losses(self):
        # match1: Marcus (home) 6, Louie (away) 10 -> Marcus lost, Louie won.
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            marcus = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
            louie = self.client.get(reverse('team_schedule_modal', args=[self.team_b.id])).json()['html']
        self.assertIn('badge bg-danger">Lost', marcus)
        self.assertNotIn('badge bg-success">Won', marcus)
        self.assertIn('badge bg-success">Won', louie)
        self.assertIn('10-6', louie)

    def test_team_schedule_modal_headline_and_rank_breakdown(self):
        # match1: Marcus (rank 2) lost to Louie (rank 4) 6-10.
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            marcus = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
            louie = self.client.get(reverse('team_schedule_modal', args=[self.team_b.id])).json()['html']

        # Big name in the header, ring with the match win percentage.
        self.assertIn('font-size:2rem', marcus)
        self.assertIn('>0%</text>', marcus)
        self.assertIn('0-1 matches', marcus)
        self.assertIn('>100%</text>', louie)
        self.assertIn('1-0 matches', louie)
        # Ring drawn to the percentage: no arc at all for 0% (no stray dot), full circle for 100%.
        self.assertNotIn('stroke-dasharray', marcus)
        self.assertIn('stroke-dasharray="282.7 282.7"', louie)
        # Per-rank strip lists every rank 5..1; Marcus's only match was vs a rank 4.
        for rank in (5, 4, 3, 2, 1):
            self.assertIn(f'Rank {rank}', marcus)
        self.assertIn('no matches', marcus)
        self.assertIn('Win rate by opponent rank', marcus)

    def test_team_schedule_modal_extras(self):
        from datetime import timedelta
        from django.utils import timezone as tz
        # An upcoming match for Marcus next week against Louie (rank 4).
        future = Week.objects.create(season=self.season, date=tz.localdate() + timedelta(days=7), number=9)
        Match.objects.create(week=future, home_team=self.team_a, away_team=self.team_b)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']

        # Form: one red dot, no streak text (needs two in a row).
        self.assertIn('data-form-strip', html)
        self.assertIn('Last 1 match<', html)
        self.assertEqual(html.count('rounded-circle bg-danger'), 1)
        self.assertNotIn('in a row', html)
        # Marcus (2) lost to Louie (4): a "getting weight" loss; the other buckets empty.
        self.assertIn('Getting weight', html)
        self.assertIn('data-weight-split', html)
        # Average margin of the 6-10 loss.
        self.assertIn('avg margin -4.0', html)
        # Next match card with the race for rank 2 vs rank 4 and the all-time record
        # (this season 0-1 plus the archived 6-10 loss).
        self.assertIn('data-next-match', html)
        self.assertIn('race 6/10', html)
        self.assertIn('all-time 0-2', html)
        # Past seasons from the archive: finish #1, rank 2, 5-2.
        self.assertIn('Past Seasons', html)
        self.assertIn('Archived Season 1', html)
        self.assertIn('#1', html)
        self.assertIn('5-2', html)

    def test_team_schedule_modal_rank_outlook(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
        # 6-10 is 16 games at 38%: holding, with the reason and the grey bar glyph.
        self.assertIn('data-rank-outlook="hold"', html)
        self.assertIn('Holding', html)
        self.assertIn('Winning 38% of games', html)
        self.assertIn('aria-label="Holding"', html)

        # Give Louie (rank 4) a dominant season giving weight -> trending up with the green chevron.
        for i in range(6):
            w = Week.objects.create(season=self.season, date=date(2026, 3, 1 + i), number=20 + i)
            MatchResult.objects.create(
                match=Match.objects.create(week=w, home_team=self.team_b, away_team=self.team_a),
                home_team_score=3, away_team_score=0,
            )
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            louie = self.client.get(reverse('team_schedule_modal', args=[self.team_b.id])).json()['html']
            marcus = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
        self.assertIn('data-rank-outlook="up"', louie)
        self.assertIn('aria-label="Trending up"', louie)
        self.assertIn('stroke="#198754"', louie)
        # Marcus (rank 2) lost them all -> trending down with the red chevron.
        self.assertIn('data-rank-outlook="down"', marcus)
        self.assertIn('aria-label="Trending down"', marcus)
        self.assertIn('stroke="#dc3545"', marcus)

    def test_team_schedule_modal_streak(self):
        # Two more losses for Marcus -> "Lost 3 in a row".
        w = Week.objects.create(season=self.season, date=date(2026, 1, 26), number=4)
        MatchResult.objects.create(
            match=Match.objects.create(week=w, home_team=self.team_a, away_team=self.team_c),
            home_team_score=1, away_team_score=3,
        )
        w2 = Week.objects.create(season=self.season, date=date(2026, 2, 2), number=5)
        MatchResult.objects.create(
            match=Match.objects.create(week=w2, home_team=self.team_d, away_team=self.team_a),
            home_team_score=3, away_team_score=0,
        )
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
        self.assertIn('Lost 3 in a row', html)
        self.assertIn('Last 3 matches', html)
        self.assertEqual(html.count('rounded-circle bg-danger'), 3)
        # Marcus (2) vs Beau (1) was giving weight; vs Danni (3) getting weight.
        self.assertIn('Giving weight', html)

    def test_team_schedule_modal_headline_ignores_former_players(self):
        from results.models import MatchResult as MR
        # Credit match1 to a former holder of Marcus's spot: it must not count.
        former = Player.objects.create(league=self.league, name='Old Marcus')
        MR.objects.filter(match=self.match1).update(home_player=former)
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
        self.assertIn('>-</text>', html)
        self.assertIn('0-0 matches', html)
        self.assertIn('played by Old Marcus', html)

    def test_team_schedule_modal_shows_scheduled_and_entered_dates(self):
        from django.utils import timezone
        from django.utils.formats import date_format

        # The week is dated 2026-01-05 but the score was entered today.
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(reverse('team_schedule_modal', args=[self.team_a.id])).json()['html']
        self.assertIn('<th>Scheduled</th>', html)
        self.assertIn('<th>Entered</th>', html)
        self.assertIn(date_format(date(2026, 1, 5)), html)
        self.assertIn(date_format(timezone.localdate()), html)

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

    def test_archived_seasons_page_renders_one_pocket_archive(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_seasons'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Final Standings')
            self.assertContains(response, 'Match Results')
            # Standings ordered by archived league rank, with skill rank shown.
            self.assertContains(response, 'Marcus')
            self.assertContains(response, 'Louie')
            # Race and skill ranks for the archived match.
            self.assertContains(response, '6/10')
            self.assertContains(response, 'Marcus (2)')
            self.assertNotContains(response, 'only available for pool leagues')

            response = self.client.get(reverse('archived_seasons'), {'season': self.archived_season.id})
            self.assertEqual(response.status_code, 200)

    def test_archived_seasons_page_never_lists_other_leagues(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_seasons'))
            self.assertNotContains(response, 'Other Archive')
            self.assertNotContains(response, 'Other Team')

            # Asking for another league's season falls back to our own.
            response = self.client.get(reverse('archived_seasons'), {'season': self.other_archived_season.id})
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'Other Team')
            self.assertContains(response, 'Marcus')

    def test_archived_standings_modal(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_standings_modal'))
        self.assertEqual(response.status_code, 200)
        html = response.json()['html']
        self.assertIn('Marcus', html)
        self.assertNotIn('Other Team', html)

    def test_archived_player_history_modal(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(
                reverse('archived_player_history', args=[self.archived_season.id, 'Marcus']),
            )
        self.assertEqual(response.status_code, 200)
        html = response.json()['html']
        self.assertIn('Louie (4)', html)
        self.assertIn('6/10', html)
        self.assertIn('badge bg-danger">Lost', html)
        # Scheduled week date and the (later) date the score went in.
        self.assertIn('<th>Entered</th>', html)
        self.assertIn('June 2, 2025', html)
        self.assertIn('June 9, 2025', html)

    def test_archived_player_history_is_scoped_to_active_league(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(
                reverse('archived_player_history', args=[self.other_archived_season.id, 'Marcus']),
            )
        self.assertEqual(response.status_code, 404)

    def test_home_page_links_to_archive_page(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('home'))
        self.assertContains(response, 'href="/archived-seasons/"')


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
        self.assertNotIn('Past Seasons', response.json()['html'])

    def test_player_scores_modal_shows_linked_past_seasons(self):
        old = ArchivedSeason.objects.create(
            league=self.league, name='2025 Spring', results_type=League.ResultsType.EIGHT_BALL, team_size=3,
        )
        ArchivedPlayer.objects.create(
            archived_season=old, player=self.home_player, player_name='Alice (old name)',
            team_name='Old Team', games_won=30, games_lost=10, run_outs=2, sweeps=3, eight_on_the_breaks=1,
        )
        # An unlinked row with the same name is not hers.
        ArchivedPlayer.objects.create(archived_season=old, player_name='Alice', team_name='Impostors', games_won=99)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('player_scores_modal', args=[self.home_player.id]))
        html = response.json()['html']
        self.assertIn('Past Seasons', html)
        self.assertIn('2025 Spring', html)
        self.assertIn('Old Team', html)
        self.assertIn('Career (archived)', html)
        self.assertIn('75.0%', html)
        self.assertNotIn('Impostors', html)

    def test_archived_seasons_page_for_eight_ball(self):
        archived_season = ArchivedSeason.objects.create(
            league=self.league, name='2025 Fall', results_type=League.ResultsType.EIGHT_BALL, team_size=3,
        )
        ArchivedTeam.objects.create(
            archived_season=archived_season, team_name='Home Team', matches_won=3, matches_lost=1,
            league_rank=1, captain_name='Alice',
        )
        ArchivedPlayer.objects.create(
            archived_season=archived_season, player_name='Alice', team_name='Home Team',
            games_won=7, games_lost=2, male=False,
        )
        archived_match = ArchivedMatch.objects.create(
            archived_season=archived_season, date=date(2025, 9, 8), week_number=1,
            home_team_name='Home Team', away_team_name='Away Team', home_team_score=6, away_team_score=3,
            completed=True,
        )
        ArchivedPlayerMatchResult.objects.create(
            archived_match=archived_match, player_name='Alice', team_name='Home Team',
            wins=3, losses=0, runouts=1, won_all_games=True,
        )

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_seasons'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Archived Team Standings')
            self.assertContains(response, 'Captain: Alice')
            self.assertContains(response, 'Match Results')
            self.assertContains(response, '6-3')
            self.assertNotContains(response, 'Skill')

            response = self.client.get(
                reverse('archived_player_history', args=[archived_season.id, 'Alice']),
            )
            self.assertEqual(response.status_code, 200)
            html = response.json()['html']
            self.assertIn('Away Team', html)
            self.assertIn('Run Outs', html)
            self.assertNotIn('Race', html)

    def test_archived_seasons_page_without_archives(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('archived_seasons'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No archived seasons available')

    def test_bye_week_opponent_is_not_a_link(self):
        # Week 2 has no match for the team, so the schedule shows a Bye row.
        Week.objects.create(season=self.season, date=date(2026, 1, 12), number=2)

        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.home_team.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Bye')
        # There is no page for "Bye" — the row must not render a hyperlink.
        # (The bug rendered <a href="/teams//">Bye</a> with an empty id.)
        self.assertNotContains(response, 'href="/teams//"')


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


class TeamPlayerStatsRosterTests(ViewTestCase):
    """The team page should always list the roster, zero-filled when a player
    has no results yet — including between seasons (no active season at all)."""

    def setUp(self):
        super().setUp()
        self.league = make_league(name='Roster League', results_type=League.ResultsType.EIGHT_BALL, team_size=3)
        self.venue = make_venue(self.league)
        self.team = make_team(self.league, self.venue, 'Roster Team')
        self.player = Player.objects.create(league=self.league, name='Zoe NoResults', team=self.team, male=False)

    def test_players_listed_with_zeros_when_no_active_season(self):
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Zoe NoResults')
        stats = response.context['team_player_stats']
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]['wins'], 0)
        self.assertEqual(stats[0]['losses'], 0)
        self.assertEqual(stats[0]['runs'], 0)
        self.assertEqual(stats[0]['percentage'], 0.0)

    def test_players_listed_with_zeros_when_season_has_no_results(self):
        Season.objects.create(league=self.league, name='Fresh Season', status=Season.Status.ACTIVE)
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            response = self.client.get(reverse('team_detail', args=[self.team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Zoe NoResults')
        stats = response.context['team_player_stats']
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]['wins'], 0)
