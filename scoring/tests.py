from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from core.models import League, Player, Team, Venue
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season, Week

from .models import ScoringProfile


def make_league(**kwargs):
    defaults = {
        'name': 'EMC Fun Pool League',
        'team_size': 5,
        'results_type': League.ResultsType.EIGHT_BALL,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def make_venue(league, name='Cue Club'):
    return Venue.objects.create(
        league=league,
        name=name,
        phone='555-1234',
        address='123 Main St',
        number_of_tables=4,
        max_home_teams=2,
        min_home_teams=1,
    )


class ScoringBase(TestCase):
    def setUp(self):
        self.league = make_league()
        self.venue = make_venue(self.league)
        self.home_team = Team.objects.create(league=self.league, venue=self.venue, name='Sharks')
        self.away_team = Team.objects.create(league=self.league, venue=self.venue, name='Jets')
        self.other_team = Team.objects.create(league=self.league, venue=self.venue, name='Others')

        self.home_players = [
            Player.objects.create(league=self.league, team=self.home_team, name=f'Home P{i}')
            for i in range(1, 6)
        ]
        self.away_players = [
            Player.objects.create(league=self.league, team=self.away_team, name=f'Away P{i}')
            for i in range(1, 6)
        ]
        self.other_player = Player.objects.create(
            league=self.league, team=self.other_team, name='Other P1'
        )

        self.season = Season.objects.create(
            league=self.league, name='S1', status=Season.Status.ACTIVE
        )
        self.week = Week.objects.create(
            season=self.season, date=timezone.localdate(), number=1
        )
        self.match = Match.objects.create(
            week=self.week, home_team=self.home_team, away_team=self.away_team
        )

        self.client = Client()

    def make_captain(self, player, approved=True, email=None):
        email = email or f'{player.name.replace(" ", "").lower()}@example.com'
        user = User.objects.create_user(username=email, email=email, password='pw12345!')
        profile = ScoringProfile.objects.create(
            user=user,
            league=self.league,
            player=player,
            role=ScoringProfile.Role.CAPTAIN,
            is_approved=approved,
        )
        return user, profile

    def make_admin(self, approved=True):
        user = User.objects.create_user(
            username='admin@example.com', email='admin@example.com', password='pw12345!'
        )
        profile = ScoringProfile.objects.create(
            user=user,
            league=self.league,
            role=ScoringProfile.Role.ADMIN,
            is_approved=approved,
        )
        return user, profile


class SignupTests(ScoringBase):
    def test_signup_creates_user_and_pending_profile(self):
        response = self.client.post('/score/signup/', {
            'email': 'newcap@example.com',
            'password1': 'Str0ngPass!x',
            'password2': 'Str0ngPass!x',
            'player': self.home_players[0].pk,
        })
        self.assertRedirects(response, '/score/pending/')

        user = User.objects.get(username='newcap@example.com')
        profile = user.scoring_profile
        self.assertFalse(profile.is_approved)
        self.assertEqual(profile.player, self.home_players[0])
        self.assertEqual(profile.league, self.league)

    def test_duplicate_email_rejected(self):
        self.make_captain(self.home_players[0], email='dup@example.com')
        response = self.client.post('/score/signup/', {
            'email': 'dup@example.com',
            'password1': 'Str0ngPass!x',
            'password2': 'Str0ngPass!x',
            'player': self.home_players[1].pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')


class ApprovalGateTests(ScoringBase):
    def test_unapproved_captain_redirected_to_pending(self):
        user, _ = self.make_captain(self.home_players[0], approved=False)
        self.client.force_login(user)

        response = self.client.get('/score/')
        self.assertRedirects(response, '/score/pending/')

        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertRedirects(response, '/score/pending/')

    def test_approved_captain_sees_match_list(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.get('/score/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sharks vs Jets')


class ScoreEntryPermissionTests(ScoringBase):
    def test_captain_cannot_score_other_teams_match(self):
        user, _ = self.make_captain(self.other_player)
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertRedirects(response, '/score/')

    def test_captain_sees_only_own_team_section(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertEqual(response.status_code, 200)
        sections = response.context['sections']
        self.assertEqual([s['team'].id for s in sections], [self.home_team.id])

    def test_admin_sees_both_team_sections(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertEqual(response.status_code, 200)
        sections = response.context['sections']
        self.assertEqual(
            {s['team'].id for s in sections},
            {self.home_team.id, self.away_team.id},
        )


class ScoreSaveTests(ScoringBase):
    def _post_home_scores(self):
        data = {}
        for i, player in enumerate(self.home_players):
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = '3' if i == 0 else '2'
            data[f'runouts_{player.id}'] = '1' if i == 0 else '0'
            data[f'eights_{player.id}'] = '0'
        return self.client.post(f'/score/match/{self.match.pk}/', data)

    def test_captain_save_creates_player_results_with_auto_losses(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self._post_home_scores()
        self.assertRedirects(response, '/score/')

        result = MatchResult.objects.get(match=self.match)
        rows = result.player_results.filter(represented_team=self.home_team)
        self.assertEqual(rows.count(), 5)

        top = rows.get(player=self.home_players[0])
        self.assertEqual(top.wins, 3)
        self.assertEqual(top.losses, 2)  # team_size 5 - 3 wins
        self.assertEqual(top.runouts, 1)

    def test_wins_above_team_size_rejected(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        player = self.home_players[0]
        response = self.client.post(f'/score/match/{self.match.pk}/', {
            f'played_{player.id}': 'on',
            f'wins_{player.id}': '9',
            f'runouts_{player.id}': '0',
            f'eights_{player.id}': '0',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            PlayerMatchResult.objects.filter(match_result__match=self.match).exists()
        )

    def test_second_captain_save_keeps_first_sides_rows(self):
        home_user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(home_user)
        self._post_home_scores()

        away_user, _ = self.make_captain(self.away_players[0])
        self.client.force_login(away_user)
        player = self.away_players[0]
        self.client.post(f'/score/match/{self.match.pk}/', {
            f'played_{player.id}': 'on',
            f'wins_{player.id}': '2',
            f'runouts_{player.id}': '0',
            f'eights_{player.id}': '0',
        })

        result = MatchResult.objects.get(match=self.match)
        self.assertEqual(
            result.player_results.filter(represented_team=self.home_team).count(), 5
        )
        self.assertEqual(
            result.player_results.filter(represented_team=self.away_team).count(), 1
        )

    def test_unchecking_played_removes_row(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        self._post_home_scores()

        # Re-save with only the first player marked as played.
        player = self.home_players[0]
        self.client.post(f'/score/match/{self.match.pk}/', {
            f'played_{player.id}': 'on',
            f'wins_{player.id}': '3',
            f'runouts_{player.id}': '0',
            f'eights_{player.id}': '0',
        })

        result = MatchResult.objects.get(match=self.match)
        self.assertEqual(
            result.player_results.filter(represented_team=self.home_team).count(), 1
        )


class CrossSideValidationTests(ScoringBase):
    def _post_side(self, players, team_wins):
        """Post scores for a full side; team_wins distributed to first player."""
        data = {}
        for i, player in enumerate(players):
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = str(team_wins[i])
            data[f'runouts_{player.id}'] = '0'
            data[f'eights_{player.id}'] = '0'
        return self.client.post(f'/score/match/{self.match.pk}/', data, follow=True)

    def test_consistent_sides_no_warning(self):
        home_user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(home_user)
        self._post_side(self.home_players, [3, 3, 3, 3, 3])  # 15 wins

        away_user, _ = self.make_captain(self.away_players[0])
        self.client.force_login(away_user)
        response = self._post_side(self.away_players, [2, 2, 2, 2, 2])  # 10 wins; 15+10=25 ✓

        message_levels = [m.level_tag for m in response.context['messages']]
        self.assertIn('success', message_levels)
        self.assertNotIn('warning', message_levels)

    def test_mismatched_totals_warns_second_captain(self):
        home_user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(home_user)
        self._post_side(self.home_players, [3, 3, 3, 3, 3])  # 15 wins

        away_user, _ = self.make_captain(self.away_players[0])
        self.client.force_login(away_user)
        response = self._post_side(self.away_players, [3, 3, 3, 3, 3])  # 15; 30 != 25

        warnings = [str(m) for m in response.context['messages'] if m.level_tag == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('do not equal', warnings[0])

    def test_uneven_player_counts_warns(self):
        home_user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(home_user)
        self._post_side(self.home_players, [3, 3, 3, 3, 3])

        away_user, _ = self.make_captain(self.away_players[0])
        self.client.force_login(away_user)
        player = self.away_players[0]
        response = self.client.post(f'/score/match/{self.match.pk}/', {
            f'played_{player.id}': 'on',
            f'wins_{player.id}': '2',
            f'runouts_{player.id}': '0',
            f'eights_{player.id}': '0',
        }, follow=True)

        warnings = [str(m) for m in response.context['messages'] if m.level_tag == 'warning']
        self.assertTrue(any('same count' in w for w in warnings))

    def test_one_side_only_no_warning(self):
        home_user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(home_user)
        response = self._post_side(self.home_players, [3, 3, 3, 3, 3])

        message_levels = [m.level_tag for m in response.context['messages']]
        self.assertNotIn('warning', message_levels)


class OpponentVisibilityTests(ScoringBase):
    def test_captain_sees_opponent_rows_read_only(self):
        result = MatchResult.objects.create(match=self.match)
        PlayerMatchResult.objects.create(
            match_result=result, player=self.away_players[0],
            represented_team=self.away_team, wins=4, runouts=2, eight_on_the_breaks=1,
        )

        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/')

        readonly = response.context['readonly_sections']
        self.assertEqual(len(readonly), 1)
        self.assertEqual(readonly[0]['team'], self.away_team)
        self.assertEqual(readonly[0]['rows'][0]['wins'], 4)
        self.assertEqual(readonly[0]['rows'][0]['runouts'], 2)
        self.assertEqual(readonly[0]['rows'][0]['eights'], 1)
        # Editable sections still limited to own team.
        self.assertEqual(
            [s['team'].id for s in response.context['sections']],
            [self.home_team.id],
        )

    def test_admin_has_no_readonly_sections(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertEqual(response.context['readonly_sections'], [])


class MatchListTests(ScoringBase):
    def test_fully_scored_match_not_in_needs_score(self):
        result = MatchResult.objects.create(match=self.match)
        PlayerMatchResult.objects.create(
            match_result=result, player=self.home_players[0],
            represented_team=self.home_team, wins=3,
        )
        PlayerMatchResult.objects.create(
            match_result=result, player=self.away_players[0],
            represented_team=self.away_team, wins=2,
        )

        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        response = self.client.get('/score/')
        self.assertEqual(response.context['needs_score'], [])

    def test_half_scored_match_still_needs_score(self):
        result = MatchResult.objects.create(match=self.match)
        PlayerMatchResult.objects.create(
            match_result=result, player=self.home_players[0],
            represented_team=self.home_team, wins=3,
        )

        user, _ = self.make_captain(self.away_players[0])
        self.client.force_login(user)
        response = self.client.get('/score/')
        self.assertEqual(len(response.context['needs_score']), 1)

    def test_admin_sees_all_league_matches(self):
        third_team = Team.objects.create(league=self.league, venue=self.venue, name='Thirds')
        Match.objects.create(week=self.week, home_team=self.other_team, away_team=third_team)

        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self.client.get('/score/')
        self.assertEqual(len(response.context['needs_score']), 2)


class StaffAutoProvisionTests(ScoringBase):
    def test_staff_user_gets_admin_profile_automatically(self):
        staff = User.objects.create_user(
            username='siteadmin', email='siteadmin@example.com',
            password='pw12345!', is_staff=True,
        )
        self.client.force_login(staff)

        response = self.client.get('/score/')
        self.assertEqual(response.status_code, 200)

        profile = ScoringProfile.objects.get(user=staff)
        self.assertEqual(profile.role, ScoringProfile.Role.ADMIN)
        self.assertTrue(profile.is_approved)
        self.assertEqual(profile.league, self.league)

    def test_non_staff_user_without_profile_sees_no_account_page(self):
        plain = User.objects.create_user(
            username='random@example.com', email='random@example.com', password='pw12345!'
        )
        self.client.force_login(plain)

        response = self.client.get('/score/', follow=True)
        self.assertContains(response, 'No scoring account')
        self.assertFalse(ScoringProfile.objects.filter(user=plain).exists())


class PwaEndpointTests(ScoringBase):
    def test_manifest_served(self):
        response = self.client.get('/score/manifest.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['start_url'], '/score/')

    def test_service_worker_served_as_javascript(self):
        response = self.client.get('/score/sw.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/javascript')
