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

    def test_captain_redirected_to_game_flow(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertRedirects(
            response, f'/score/match/{self.match.pk}/games/',
            fetch_redirect_response=False,
        )

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
    """Totals-grid entry, now an admin-only path (captains use the game flow)."""

    def _admin_post(self, players_wins):
        data = {}
        for player, wins, runouts, eights in players_wins:
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = str(wins)
            data[f'runouts_{player.id}'] = str(runouts)
            data[f'eights_{player.id}'] = str(eights)
        return self.client.post(f'/score/match/{self.match.pk}/', data)

    def test_admin_save_creates_player_results_with_auto_losses(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        rows = [(self.home_players[0], 3, 1, 0)] + [
            (p, 2, 0, 0) for p in self.home_players[1:]
        ]
        response = self._admin_post(rows)
        self.assertRedirects(response, '/score/')

        result = MatchResult.objects.get(match=self.match)
        saved = result.player_results.filter(represented_team=self.home_team)
        self.assertEqual(saved.count(), 5)

        top = saved.get(player=self.home_players[0])
        self.assertEqual(top.wins, 3)
        self.assertEqual(top.losses, 2)  # team_size 5 - 3 wins
        self.assertEqual(top.runouts, 1)

    def test_wins_above_team_size_rejected(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        response = self._admin_post([(self.home_players[0], 9, 0, 0)])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            PlayerMatchResult.objects.filter(match_result__match=self.match).exists()
        )

    def test_unchecking_played_removes_row(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        self._admin_post([(p, 2, 0, 0) for p in self.home_players])

        # Re-save with only the first player marked as played.
        self._admin_post([(self.home_players[0], 3, 0, 0)])

        result = MatchResult.objects.get(match=self.match)
        self.assertEqual(
            result.player_results.filter(represented_team=self.home_team).count(), 1
        )


class CrossSideValidationTests(ScoringBase):
    """Admin totals entry warns when the two sides' numbers can't both be right."""

    def _post_both_sides(self, home_wins, away_wins):
        data = {}
        for player, wins in zip(self.home_players, home_wins):
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = str(wins)
            data[f'runouts_{player.id}'] = '0'
            data[f'eights_{player.id}'] = '0'
        for player, wins in zip(self.away_players, away_wins):
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = str(wins)
            data[f'runouts_{player.id}'] = '0'
            data[f'eights_{player.id}'] = '0'
        return self.client.post(f'/score/match/{self.match.pk}/', data, follow=True)

    def test_consistent_sides_no_warning(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self._post_both_sides([3, 3, 3, 3, 3], [2, 2, 2, 2, 2])  # 15+10=25 ✓

        message_levels = [m.level_tag for m in response.context['messages']]
        self.assertIn('success', message_levels)
        self.assertNotIn('warning', message_levels)

    def test_mismatched_totals_warns(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self._post_both_sides([3, 3, 3, 3, 3], [3, 3, 3, 3, 3])  # 30 != 25

        warnings = [str(m) for m in response.context['messages'] if m.level_tag == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('do not equal', warnings[0])

    def test_uneven_player_counts_warns(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        data = {}
        for player in self.home_players:
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = '3'
        away = self.away_players[0]
        data[f'played_{away.id}'] = 'on'
        data[f'wins_{away.id}'] = '2'
        response = self.client.post(f'/score/match/{self.match.pk}/', data, follow=True)

        warnings = [str(m) for m in response.context['messages'] if m.level_tag == 'warning']
        self.assertTrue(any('same count' in w for w in warnings))

    def test_one_side_only_no_warning(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        data = {}
        for player in self.home_players:
            data[f'played_{player.id}'] = 'on'
            data[f'wins_{player.id}'] = '3'
        response = self.client.post(f'/score/match/{self.match.pk}/', data, follow=True)

        message_levels = [m.level_tag for m in response.context['messages']]
        self.assertNotIn('warning', message_levels)


class OpponentVisibilityTests(ScoringBase):
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


class SubTests(ScoringBase):
    def setUp(self):
        super().setUp()
        self.sub = Player.objects.create(league=self.league, team=None, name='Sub Sally')

    def test_admin_can_add_sub_from_unassigned_players(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        response = self.client.post(f'/score/match/{self.match.pk}/', {
            f'sub_player_{self.home_team.id}_1': str(self.sub.id),
            f'sub_wins_{self.home_team.id}_1': '4',
            f'sub_runouts_{self.home_team.id}_1': '1',
            f'sub_eights_{self.home_team.id}_1': '0',
        })
        self.assertRedirects(response, '/score/')

        row = PlayerMatchResult.objects.get(
            match_result__match=self.match, player=self.sub,
        )
        self.assertEqual(row.represented_team, self.home_team)
        self.assertEqual(row.wins, 4)
        self.assertEqual(row.losses, 1)
        self.assertEqual(row.runouts, 1)

    def test_saved_sub_appears_as_editable_row_on_reload(self):
        result = MatchResult.objects.create(match=self.match)
        PlayerMatchResult.objects.create(
            match_result=result, player=self.sub,
            represented_team=self.home_team, wins=2,
        )

        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/')

        section = response.context['sections'][0]
        sub_rows = [r for r in section['rows'] if r['player'] == self.sub]
        self.assertEqual(len(sub_rows), 1)
        self.assertTrue(sub_rows[0]['played'])
        self.assertEqual(sub_rows[0]['wins'], 2)

    def test_assigned_player_rejected_as_sub(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        # Away player is assigned to a team, so not an eligible sub.
        response = self.client.post(f'/score/match/{self.match.pk}/', {
            f'sub_player_{self.home_team.id}_1': str(self.away_players[0].id),
            f'sub_wins_{self.home_team.id}_1': '3',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            PlayerMatchResult.objects.filter(match_result__match=self.match).exists()
        )

    def test_sub_choices_offered_in_context(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertIn(self.sub, response.context['sub_choices'])


class AddPlayerTests(ScoringBase):
    def test_captain_adds_sub_player(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.post('/score/players/new/', {
            'name': 'Walk-in Wanda',
            'gender': 'female',
            'assignment': 'sub',
        })
        self.assertRedirects(response, '/score/')

        player = Player.objects.get(league=self.league, name='Walk-in Wanda')
        self.assertIsNone(player.team)
        self.assertFalse(player.male)

    def test_captain_adds_player_to_own_team(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        self.client.post('/score/players/new/', {
            'name': 'New Teammate',
            'gender': 'male',
            'assignment': 'team',
        })

        player = Player.objects.get(league=self.league, name='New Teammate')
        self.assertEqual(player.team, self.home_team)

    def test_duplicate_name_rejected(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.post('/score/players/new/', {
            'name': 'home p1',  # case-insensitive match on existing Home P1
            'gender': 'male',
            'assignment': 'sub',
        }, follow=True)
        self.assertContains(response, 'already exists')
        self.assertEqual(
            Player.objects.filter(league=self.league, name__iexact='home p1').count(), 1
        )

    def test_next_url_returns_to_score_entry(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.post('/score/players/new/', {
            'name': 'Return Ricky',
            'gender': 'male',
            'assignment': 'sub',
            'next': f'/score/match/{self.match.pk}/lineup/',
        })
        self.assertRedirects(
            response, f'/score/match/{self.match.pk}/lineup/',
            fetch_redirect_response=False,
        )

    def test_external_next_url_ignored(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.post('/score/players/new/', {
            'name': 'Odd Redirect',
            'gender': 'male',
            'assignment': 'sub',
            'next': 'https://evil.example.com/',
        })
        self.assertRedirects(response, '/score/')

    def test_new_sub_appears_in_lineup_choices(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        self.client.post('/score/players/new/', {
            'name': 'Sub Steve',
            'gender': 'male',
            'assignment': 'sub',
        })
        response = self.client.get(f'/score/match/{self.match.pk}/lineup/')
        home_block = next(
            b for b in response.context['team_blocks'] if b['team'] == self.home_team
        )
        self.assertIn('Sub Steve', [p.name for p in home_block['choices']])

    def test_unapproved_captain_cannot_add_player(self):
        user, _ = self.make_captain(self.home_players[0], approved=False)
        self.client.force_login(user)

        response = self.client.post('/score/players/new/', {
            'name': 'Should Not Exist',
            'gender': 'male',
            'assignment': 'sub',
        })
        self.assertRedirects(response, '/score/pending/')
        self.assertFalse(
            Player.objects.filter(league=self.league, name='Should Not Exist').exists()
        )


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


class GameFlowTests(ScoringBase):
    """Round-robin lineup + game-by-game entry, mirroring the paper sheet."""

    def _set_lineups(self):
        data = {}
        for i, player in enumerate(self.home_players, start=1):
            data[f'lineup_{self.home_team.id}_{i}'] = str(player.id)
        for i, player in enumerate(self.away_players, start=1):
            data[f'lineup_{self.away_team.id}_{i}'] = str(player.id)
        return self.client.post(f'/score/match/{self.match.pk}/lineup/', data)

    def _post_all_games(self, winner='home', flags=None):
        """Record every game with the given winner. flags maps
        (round, position) -> dict of extra POST fields."""
        flags = flags or {}
        data = {}
        for rnd in range(1, 6):
            for pos in range(1, 6):
                data[f'winner_{rnd}_{pos}'] = winner
                for key, value in flags.get((rnd, pos), {}).items():
                    data[f'{key}_{rnd}_{pos}'] = value
        return self.client.post(f'/score/match/{self.match.pk}/games/', data)

    def test_rotation_matches_paper_sheet(self):
        from scoring.models import GameResult
        # Round 1: 1:A 2:B 3:C 4:D 5:E — round 2 shifts by one: 1:B ... 5:A
        self.assertEqual(GameResult.away_position_for(1, 1, 5), 1)
        self.assertEqual(GameResult.away_position_for(5, 1, 5), 5)
        self.assertEqual(GameResult.away_position_for(1, 2, 5), 2)
        self.assertEqual(GameResult.away_position_for(5, 2, 5), 1)
        self.assertEqual(GameResult.away_position_for(1, 5, 5), 5)
        self.assertEqual(GameResult.away_position_for(5, 5, 5), 4)

    def test_games_gate_redirects_until_both_lineups_set(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/games/')
        self.assertRedirects(response, f'/score/match/{self.match.pk}/lineup/')

    def test_lineup_save_creates_slots_and_redirects_to_games(self):
        from scoring.models import LineupSlot
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self._set_lineups()
        self.assertRedirects(response, f'/score/match/{self.match.pk}/games/')
        self.assertEqual(
            LineupSlot.objects.filter(match=self.match).count(), 10
        )

    def test_lineup_rejects_duplicate_player(self):
        from scoring.models import LineupSlot
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        data = {}
        for i in range(1, 6):
            data[f'lineup_{self.home_team.id}_{i}'] = str(self.home_players[0].id)
        response = self.client.post(f'/score/match/{self.match.pk}/lineup/', data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LineupSlot.objects.filter(match=self.match).count(), 0)

    def test_partial_lineup_rejected(self):
        from scoring.models import LineupSlot
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        data = {
            f'lineup_{self.home_team.id}_1': str(self.home_players[0].id),
            f'lineup_{self.home_team.id}_2': str(self.home_players[1].id),
        }
        response = self.client.post(f'/score/match/{self.match.pk}/lineup/', data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LineupSlot.objects.filter(match=self.match).count(), 0)

    def test_full_game_entry_builds_match_totals(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        self._set_lineups()

        # Home wins every game; runout on round 1 game 1, 8-break on round 2 game 3.
        response = self._post_all_games('home', flags={
            (1, 1): {'ro': 'on'},
            (2, 3): {'eb': 'on'},
        })
        self.assertRedirects(response, '/score/')

        result = MatchResult.objects.get(match=self.match)
        home_rows = result.player_results.filter(represented_team=self.home_team)
        away_rows = result.player_results.filter(represented_team=self.away_team)
        self.assertEqual(home_rows.count(), 5)
        self.assertEqual(away_rows.count(), 5)

        for row in home_rows:
            self.assertEqual(row.wins, 5)
            self.assertEqual(row.losses, 0)
            self.assertTrue(row.won_all_games)  # 5 and 0
        for row in away_rows:
            self.assertEqual(row.wins, 0)
            self.assertEqual(row.losses, 5)

        p1 = home_rows.get(player=self.home_players[0])
        self.assertEqual(p1.runouts, 1)
        p3 = home_rows.get(player=self.home_players[2])
        self.assertEqual(p3.eight_on_the_breaks, 1)

    def test_partial_game_entry_creates_no_totals(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        self._set_lineups()

        response = self.client.post(f'/score/match/{self.match.pk}/games/', {
            'winner_1_1': 'home',
            'winner_1_2': 'away',
        })
        self.assertRedirects(response, f'/score/match/{self.match.pk}/games/')

        self.assertFalse(MatchResult.objects.filter(match=self.match).exists())
        # Match still shows as needing a score.
        list_response = self.client.get('/score/')
        self.assertEqual(len(list_response.context['needs_score']), 1)

    def test_split_match_totals_correct(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        self._set_lineups()

        # Home wins rounds 1-3, away wins rounds 4-5 → home players 3-2, away 2-3.
        data = {}
        for rnd in range(1, 6):
            for pos in range(1, 6):
                data[f'winner_{rnd}_{pos}'] = 'home' if rnd <= 3 else 'away'
        self.client.post(f'/score/match/{self.match.pk}/games/', data)

        result = MatchResult.objects.get(match=self.match)
        for row in result.player_results.filter(represented_team=self.home_team):
            self.assertEqual(row.wins, 3)
            self.assertEqual(row.losses, 2)
        for row in result.player_results.filter(represented_team=self.away_team):
            self.assertEqual(row.wins, 2)
            self.assertEqual(row.losses, 3)

    def test_sub_allowed_in_lineup(self):
        from scoring.models import LineupSlot
        sub = Player.objects.create(league=self.league, team=None, name='Lineup Sub')
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        data = {}
        for i, player in enumerate(self.home_players[:4], start=1):
            data[f'lineup_{self.home_team.id}_{i}'] = str(player.id)
        data[f'lineup_{self.home_team.id}_5'] = str(sub.id)
        self.client.post(f'/score/match/{self.match.pk}/lineup/', data)

        slot = LineupSlot.objects.get(match=self.match, team=self.home_team, position=5)
        self.assertEqual(slot.player, sub)

    def test_opposing_team_player_rejected_in_lineup(self):
        from scoring.models import LineupSlot
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        data = {}
        for i, player in enumerate(self.home_players[:4], start=1):
            data[f'lineup_{self.home_team.id}_{i}'] = str(player.id)
        data[f'lineup_{self.home_team.id}_5'] = str(self.away_players[0].id)
        response = self.client.post(f'/score/match/{self.match.pk}/lineup/', data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LineupSlot.objects.filter(match=self.match).count(), 0)


class AdminViewSwitchTests(ScoringBase):
    def _set_lineups(self):
        data = {}
        for i, player in enumerate(self.home_players, start=1):
            data[f'lineup_{self.home_team.id}_{i}'] = str(player.id)
        for i, player in enumerate(self.away_players, start=1):
            data[f'lineup_{self.away_team.id}_{i}'] = str(player.id)
        self.client.post(f'/score/match/{self.match.pk}/lineup/', data)

    def test_admin_sees_admin_view_link_on_lineup_and_games(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/lineup/')
        self.assertContains(response, 'Admin view (totals entry)')

        self._set_lineups()
        response = self.client.get(f'/score/match/{self.match.pk}/games/')
        self.assertContains(response, 'Admin view (totals entry)')

    def test_captain_does_not_see_admin_view_link(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/lineup/')
        self.assertNotContains(response, 'Admin view (totals entry)')

        self._set_lineups()
        response = self.client.get(f'/score/match/{self.match.pk}/games/')
        self.assertNotContains(response, 'Admin view (totals entry)')

    def test_admin_totals_grid_links_to_game_flow(self):
        user, _ = self.make_admin()
        self.client.force_login(user)

        response = self.client.get(f'/score/match/{self.match.pk}/')
        self.assertContains(response, 'Use the round-robin sheet')


class PwaEndpointTests(ScoringBase):
    def test_manifest_served(self):
        response = self.client.get('/score/manifest.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['start_url'], '/score/')

    def test_service_worker_served_as_javascript(self):
        response = self.client.get('/score/sw.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/javascript')
