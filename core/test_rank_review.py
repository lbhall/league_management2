"""Rank review for one-pocket leagues."""
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from core.models import League, LeagueAdminAccess, Player, Team, Venue
from core.rank_review import MIN_GAMES, build_rank_review, season_choices, weight_category
from results.models import MatchResult
from scheduling.models import Match, Season, Week


def make_league(name='Bogies', results_type=League.ResultsType.ONE_POCKET, team_size=1):
    return League.objects.create(
        name=name, team_size=team_size, results_type=results_type, day_of_week=League.DayOfWeek.SUNDAY,
    )


class RankReviewBase(TestCase):
    def setUp(self):
        self.league = make_league()
        self.venue = Venue.objects.create(
            league=self.league, name='Bogies', phone='', address='', number_of_tables=9,
            max_home_teams=9, min_home_teams=1,
        )
        self.season = Season.objects.create(league=self.league, name='Mid 2026', status=Season.Status.ACTIVE)
        self.week_no = 0
        self.players = {}
        for name, rank in (('Abel', 5), ('Jim', 4), ('Mario', 3), ('Danni', 1), ('Red', 2)):
            self.players[name] = Team.objects.create(league=self.league, venue=self.venue, name=name, team_rank=rank)

    def play(self, home, away, home_score, away_score):
        self.week_no += 1
        week = Week.objects.create(season=self.season, date=date(2026, 7, self.week_no), number=self.week_no)
        match = Match.objects.create(week=week, home_team=self.players[home], away_team=self.players[away])
        MatchResult.objects.create(match=match, home_team_score=home_score, away_team_score=away_score)
        return match

    def row(self, rows, name):
        return next(r for r in rows if r['name'] == name)


class WeightCategoryTests(TestCase):
    def test_categories(self):
        self.assertEqual(weight_category(5, 3), 'giving')
        self.assertEqual(weight_category(3, 5), 'getting')
        self.assertEqual(weight_category(3, 3), 'even')
        self.assertEqual(weight_category(None, 3), 'even')


class BuildRankReviewTests(RankReviewBase):
    def test_splits_and_totals(self):
        # Mario (3) beats Abel (5) 3-1 while getting weight, loses to Danni (1) 1-3 while giving.
        self.play('Mario', 'Abel', 3, 1)
        self.play('Danni', 'Mario', 3, 1)
        self.play('Mario', 'Red', 3, 2)  # giving weight (3 vs 2)

        rows = build_rank_review(self.league, self.season)
        mario = self.row(rows, 'Mario')
        self.assertEqual((mario['matches_won'], mario['matches_lost']), (2, 1))
        self.assertEqual((mario['games_won'], mario['games_lost']), (7, 6))
        self.assertAlmostEqual(mario['game_pct'], 7 / 13)
        self.assertAlmostEqual(mario['avg_margin'], (2 - 2 + 1) / 3)
        self.assertEqual(mario['splits']['getting']['games_won'], 3)
        self.assertEqual(mario['splits']['getting']['games_lost'], 1)
        self.assertEqual(mario['splits']['giving']['matches_won'], 1)
        self.assertEqual(mario['splits']['giving']['matches_lost'], 1)
        self.assertEqual(mario['splits']['giving']['games_won'], 4)
        self.assertEqual(mario['splits']['even']['games_played'], 0)
        self.assertEqual(len(mario['split_cells']), 3)

        abel = self.row(rows, 'Abel')
        self.assertEqual(abel['splits']['giving']['games_lost'], 3)

    def test_incomplete_and_unscored_matches_are_ignored(self):
        self.play('Mario', 'Abel', 2, 1)  # not finished (nobody at 3)
        self.week_no += 1
        week = Week.objects.create(season=self.season, date=date(2026, 8, 1), number=self.week_no)
        Match.objects.create(week=week, home_team=self.players['Mario'], away_team=self.players['Jim'])
        rows = build_rank_review(self.league, self.season)
        self.assertEqual(self.row(rows, 'Mario')['games_played'], 0)

    def test_up_flag_needs_sample_and_holding_when_giving(self):
        # Red (2) wins big against everyone, including when giving weight to Danni (1).
        for _ in range(3):
            self.play('Red', 'Abel', 3, 0)
            self.play('Red', 'Danni', 3, 1)
        rows = build_rank_review(self.league, self.season)
        red = self.row(rows, 'Red')
        self.assertGreaterEqual(red['games_played'], MIN_GAMES)
        self.assertEqual(red['signal'], 'up')
        self.assertIn('giving weight', red['reason'])

    def test_no_up_flag_when_losing_while_giving_weight(self):
        for _ in range(10):
            self.play('Red', 'Abel', 3, 0)   # getting weight, dominant
        for _ in range(4):
            self.play('Danni', 'Red', 3, 2)  # giving weight, losing
        red = self.row(build_rank_review(self.league, self.season), 'Red')
        self.assertEqual(red['signal'], 'hold')
        self.assertIn('when giving weight', red['reason'])

    def test_down_flag(self):
        for _ in range(4):
            self.play('Abel', 'Jim', 0, 3)   # Abel (5) loses giving weight
            self.play('Abel', 'Mario', 1, 3)
        abel = self.row(build_rank_review(self.league, self.season), 'Abel')
        self.assertEqual(abel['signal'], 'down')

    def test_top_rank_never_flagged_up_and_bottom_never_down(self):
        for _ in range(4):
            self.play('Abel', 'Jim', 3, 0)
            self.play('Danni', 'Red', 0, 3)
        rows = build_rank_review(self.league, self.season)
        self.assertEqual(self.row(rows, 'Abel')['signal'], 'hold')
        self.assertEqual(self.row(rows, 'Danni')['signal'], 'hold')
        self.assertIn('top rank', self.row(rows, 'Abel')['reason'])

    def test_small_sample_is_not_flagged(self):
        self.play('Red', 'Abel', 3, 0)
        red = self.row(build_rank_review(self.league, self.season), 'Red')
        self.assertEqual(red['signal'], 'hold')
        self.assertEqual(red['reason'], f'Not enough games yet (3 of {MIN_GAMES})')

    def test_sorted_by_rank_then_game_pct(self):
        self.play('Jim', 'Mario', 3, 0)
        rows = build_rank_review(self.league, self.season)
        self.assertEqual([r['name'] for r in rows][:3], ['Abel', 'Jim', 'Mario'])

    def test_player_outlook_for_one_player(self):
        from core.rank_review import player_outlook
        for _ in range(3):
            self.play('Red', 'Abel', 3, 0)
            self.play('Red', 'Danni', 3, 1)
        red = Player.objects.get(league=self.league, name='Red')
        outlook = player_outlook(self.league, self.season, red.id)
        self.assertEqual(outlook['state'], 'up')
        self.assertIn('Winning', outlook['reason'])
        self.assertIsNone(player_outlook(self.league, None, red.id))
        self.assertIsNone(player_outlook(self.league, self.season, 999999))

    def test_season_choices_order(self):
        old = Season.objects.create(league=self.league, name='Early 2026', status=Season.Status.ARCHIVED)
        Week.objects.create(season=old, date=date(2026, 1, 4), number=1)
        Week.objects.create(season=self.season, date=date(2026, 7, 5), number=1)
        Season.objects.create(league=self.league, name='Late 2026', status=Season.Status.WORKING)
        self.assertEqual([s.name for s in season_choices(self.league)], ['Mid 2026', 'Late 2026', 'Early 2026'])


class RankReviewViewTests(RankReviewBase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.superuser = User.objects.create_superuser(username='root', password='pw', email='r@example.com')
        for _ in range(3):
            self.play('Red', 'Abel', 3, 0)
            self.play('Red', 'Danni', 3, 1)
        self.url = reverse('admin:core_league_rank_review', args=[self.league.pk])

    def test_superuser_sees_the_page_with_flags(self):
        self.client.force_login(self.superuser)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        red = next(r for r in response.context['rows'] if r['name'] == 'Red')
        self.assertEqual(red['signal'], 'up')
        self.assertEqual(red['game_pct_label'], '86%')
        self.assertContains(response, '<td>86%</td>', html=True)
        self.assertContains(response, 'Red')
        # The admin base prints the title once; the template must not repeat it.
        self.assertEqual(response.content.decode().count('Rank review — Bogies'), 1)
        self.assertNotContains(response, '0.857')
        self.assertContains(response, 'Mid 2026 (Active)')
        self.assertContains(response, 'Giving weight')

    def test_season_selector(self):
        old = Season.objects.create(league=self.league, name='Early 2026', status=Season.Status.ARCHIVED)
        self.client.force_login(self.superuser)
        response = self.client.get(self.url, {'season': old.pk})
        self.assertEqual(response.context['season'], old)
        self.assertTrue(all(r['signal'] == 'hold' for r in response.context['rows']))
        self.assertContains(response, 'Hold')

    def test_non_one_pocket_league_is_refused(self):
        emc = make_league(name='EMC', results_type=League.ResultsType.EIGHT_BALL, team_size=5)
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:core_league_rank_review', args=[emc.pk]))
        self.assertRedirects(response, reverse('admin:core_league_change', args=[emc.pk]))

    def test_league_change_page_links_only_for_one_pocket(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:core_league_change', args=[self.league.pk]))
        self.assertContains(response, 'Rank Review')
        emc = make_league(name='EMC', results_type=League.ResultsType.EIGHT_BALL, team_size=5)
        response = self.client.get(reverse('admin:core_league_change', args=[emc.pk]))
        self.assertNotContains(response, 'Rank Review')

    def test_scoped_admin_cannot_open_another_league(self):
        other = make_league(name='Other One Pocket')
        User = get_user_model()
        staff = User.objects.create_user(username='opr', password='pw', is_staff=True)
        LeagueAdminAccess.objects.create(user=staff, league=other)
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label='core'))
        self.client.force_login(staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)
        # Their own league works and the home page offers the button.
        response = self.client.get(reverse('admin:core_league_rank_review', args=[other.pk]))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse('admin:index'))
        self.assertContains(response, 'Rank Review')

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)
