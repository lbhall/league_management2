"""Tests for the scoresheet photo reader. The model call is stubbed; the
cross-checking, name matching, permissions and loading are exercised for real."""
import io
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from PIL import Image

from core.models import League, Player
from results.models import MatchResult, PlayerMatchResult
from scoring import scoresheet
from scoring.models import GameResult, LineupSlot, MatchEntry, ScoresheetScan
from scoring.tests import ScoringBase


def fake_image_bytes(width=200, height=100):
    image = Image.new('RGB', (width, height), (255, 255, 255))
    out = io.BytesIO()
    image.save(out, format='JPEG')
    return out.getvalue()


def sheet_data(home_names, away_names, home_marks, away_marks, **extra):
    """Build a transcript in the shape build_schema() describes."""
    data = {
        'home_team': 'Bam Bitches',
        'away_team': 'Predators',
        'date': '8/17/26',
        'home_players': [
            {'slot': str(i + 1), 'name': n, 'games': list(m), 'wins': m.count('W')}
            for i, (n, m) in enumerate(zip(home_names, home_marks))
        ],
        'away_players': [
            {'slot': chr(65 + i), 'name': n, 'games': list(m), 'wins': m.count('W')}
            for i, (n, m) in enumerate(zip(away_names, away_marks))
        ],
        'home_total': sum(m.count('W') for m in home_marks),
        'away_total': sum(m.count('W') for m in away_marks),
        'run_outs': [],
        'eight_on_breaks': [],
        'five_and_zero': [],
        'legibility': 'good',
        'notes': '',
    }
    data.update(extra)
    return data


# The second sample sheet from the league (Bam Bitches vs Predators). Every
# game cross-checks, so it's the "clean read" fixture.
SAMPLE_HOME = ['Amanda', 'Lazaro', 'Faisal', 'Eva Kaye', 'Red']
SAMPLE_AWAY = ['Bob', 'Victor', 'Mike', 'Kelly', 'Loretta']
SAMPLE_HOME_MARKS = ['WLLWW', 'WLLLL', 'LLLLW', 'WWLLL', 'WLWWW']
SAMPLE_AWAY_MARKS = ['LWWWW', 'LWLWL', 'WWWLW', 'LWWLL', 'LLWWL']


class EightBallScanBase(ScoringBase):
    """ScoringBase with an 8-ball league and rosters named like the sample sheet."""

    def setUp(self):
        super().setUp()
        self.league.results_type = League.ResultsType.EIGHT_BALL
        self.league.team_size = 5
        self.league.dual_entry_scoring = True
        self.league.save()
        for player, name in zip(self.home_players, SAMPLE_HOME):
            player.name = name
            player.save()
        for player, name in zip(self.away_players, SAMPLE_AWAY):
            player.name = name
            player.save()
        self.sub = Player.objects.create(league=self.league, name='Derek', team=None)


class ImagePrepTests(EightBallScanBase):
    def test_downscales_and_returns_jpeg(self):
        data, media_type = scoresheet.prepare_image(fake_image_bytes(4000, 3000))
        self.assertEqual(media_type, 'image/jpeg')
        image = Image.open(io.BytesIO(data))
        self.assertLessEqual(max(image.size), scoresheet.MAX_EDGE_PX)

    def test_rejects_non_image(self):
        with self.assertRaises(scoresheet.ScanError):
            scoresheet.prepare_image(b'not an image at all')

    def test_rejects_oversize_upload(self):
        with self.assertRaises(scoresheet.ScanError):
            scoresheet.prepare_image(b'0' * (scoresheet.MAX_UPLOAD_BYTES + 1))


class NameMatchingTests(EightBallScanBase):
    def test_exact_match_ignores_case_and_sub_marker(self):
        player, quality = scoresheet.match_player('derek (sub)', [self.sub])
        self.assertEqual(player, self.sub)
        self.assertEqual(quality, 'exact')

    def test_first_name_match_when_unique(self):
        eva = self.home_players[3]
        player, quality = scoresheet.match_player('Eva', self.home_players)
        self.assertEqual(player, eva)
        self.assertEqual(quality, 'partial')

    def test_ambiguous_names_are_not_matched(self):
        a = Player.objects.create(league=self.league, name='Chris Henk')
        b = Player.objects.create(league=self.league, name='Chris Newman')
        player, quality = scoresheet.match_player('Chris', [a, b])
        self.assertIsNone(player)
        self.assertEqual(quality, 'none')

    def test_unknown_name_is_not_matched(self):
        player, quality = scoresheet.match_player('Nobody', self.home_players)
        self.assertIsNone(player)
        self.assertEqual(quality, 'none')


class ReviewTests(EightBallScanBase):
    def test_clean_sheet_cross_checks_and_counts(self):
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS, run_outs=['Red'])
        review = scoresheet.build_review(data, self.match)

        self.assertEqual(review['issues'], [])
        self.assertTrue(review['ready'])
        self.assertEqual(review['home_total'], 11)
        self.assertEqual(review['away_total'], 14)
        self.assertEqual(len(review['games']), 25)
        self.assertTrue(all(g['winner'] for g in review['games']))
        self.assertEqual([r['player_name'] for r in review['home_lineup']], SAMPLE_HOME)
        self.assertEqual([r['player_name'] for r in review['away_lineup']], SAMPLE_AWAY)
        # Red's run out lands on one of Red's (position 5) wins.
        runouts = [g for g in review['games'] if g['runout']]
        self.assertEqual(len(runouts), 1)
        self.assertEqual(runouts[0]['home_position'], 5)
        self.assertEqual(runouts[0]['winner'], 'home')

    def test_disagreeing_cells_are_flagged(self):
        away_marks = list(SAMPLE_AWAY_MARKS)
        away_marks[0] = 'WWWWW'  # Bob now "wins" round 1 vs Amanda, who also has a W
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, away_marks)
        review = scoresheet.build_review(data, self.match)

        conflicts = [g for g in review['games'] if g['conflict']]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual((conflicts[0]['round'], conflicts[0]['home_position']), (1, 1))
        self.assertFalse(review['ready'])
        self.assertTrue(any('Round 1, game 1' in i for i in review['issues']))

    def test_unreadable_mark_is_filled_from_the_other_side(self):
        home_marks = list(SAMPLE_HOME_MARKS)
        home_marks[0] = '?LLWW'
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, home_marks, SAMPLE_AWAY_MARKS)
        data['home_players'][0]['wins'] = 3
        review = scoresheet.build_review(data, self.match)
        game = next(g for g in review['games'] if g['round'] == 1 and g['home_position'] == 1)
        self.assertEqual(game['winner'], 'home')
        self.assertFalse(game['conflict'])

    def test_wins_column_mismatch_is_reported(self):
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)
        data['home_players'][0]['wins'] = 4  # marks say 3
        review = scoresheet.build_review(data, self.match)
        self.assertTrue(any('Wins column says 4' in i for i in review['issues']))

    def test_unmatched_name_blocks_lineup(self):
        home = list(SAMPLE_HOME)
        home[1] = 'Somebody New'
        data = sheet_data(home, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)
        review = scoresheet.build_review(data, self.match)
        self.assertFalse(review['lineup_complete'])
        self.assertTrue(any('Somebody New' in i for i in review['issues']))

    def test_sub_is_matched_from_unassigned_players(self):
        away = list(SAMPLE_AWAY)
        away[0] = 'Derek (sub)'
        data = sheet_data(SAMPLE_HOME, away, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)
        review = scoresheet.build_review(data, self.match)
        self.assertEqual(review['away_lineup'][0]['player_id'], self.sub.id)
        self.assertTrue(review['lineup_complete'])


class ApplyTests(EightBallScanBase):
    def test_dual_entry_apply_creates_submitted_entry(self):
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS, run_outs=['Red'])
        review = scoresheet.build_review(data, self.match)
        user, _ = self.make_admin()

        complete = scoresheet.apply_review(review, self.match, user, side='home')

        self.assertTrue(complete)
        entry = MatchEntry.objects.get(match=self.match, side='home')
        self.assertEqual(entry.status, MatchEntry.Status.SUBMITTED)
        self.assertEqual(entry.submitted_by, user)
        self.assertEqual(entry.lineup.count(), 10)
        self.assertEqual(entry.games.count(), 25)
        self.assertEqual(entry.games.filter(winner='home').count(), 11)
        self.assertEqual(entry.games.filter(runout=True).count(), 1)

    def test_non_dual_apply_writes_games_directly(self):
        self.league.dual_entry_scoring = False
        self.league.save()
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)
        review = scoresheet.build_review(data, self.match)
        user, _ = self.make_admin()

        self.assertTrue(scoresheet.apply_review(review, self.match, user))
        self.assertEqual(LineupSlot.objects.filter(match=self.match).count(), 10)
        self.assertEqual(GameResult.objects.filter(match=self.match).count(), 25)

    def test_incomplete_lineup_cannot_be_applied(self):
        home = list(SAMPLE_HOME)
        home[0] = 'Mystery'
        data = sheet_data(home, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)
        review = scoresheet.build_review(data, self.match)
        user, _ = self.make_admin()
        with self.assertRaises(scoresheet.ScanError):
            scoresheet.apply_review(review, self.match, user, side='home')


FAKE_USAGE = {
    'model': 'claude-opus-5-5',
    'input_tokens': 1800,
    'output_tokens': 400,
    'cost_usd': Decimal('0.0152'),
}


@override_settings(ANTHROPIC_API_KEY='sk-ant-test', SCORESHEET_SCAN_MAX_PER_MATCH=2)
class ScanViewTests(EightBallScanBase):
    def _upload(self):
        return {'photo': io.BytesIO(fake_image_bytes())}

    def _set_lineup(self):
        for pos, (h, a) in enumerate(zip(self.home_players, self.away_players), start=1):
            LineupSlot.objects.create(match=self.match, team=self.home_team, position=pos, player=h)
            LineupSlot.objects.create(match=self.match, team=self.away_team, position=pos, player=a)

    def _post_photo(self):
        photo = io.BytesIO(fake_image_bytes())
        photo.name = 'sheet.jpg'
        return self.client.post(f'/score/match/{self.match.pk}/scan/', {'photo': photo})

    def test_captain_cannot_use_the_reader(self):
        user, _ = self.make_captain(self.home_players[0])
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/scan/')
        self.assertRedirects(response, '/score/', fetch_redirect_response=False)

    def test_hidden_when_no_api_key(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        with override_settings(ANTHROPIC_API_KEY=''):
            response = self.client.get(f'/score/match/{self.match.pk}/scan/')
            self.assertRedirects(response, '/score/', fetch_redirect_response=False)
            response = self.client.get(f'/score/match/{self.match.pk}/resolve/')
            self.assertNotContains(response, 'Read a photo of the sheet')

    def test_only_eight_ball_leagues(self):
        self.league.results_type = League.ResultsType.ONE_POCKET
        self.league.team_size = 1
        self.league.save()
        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/scan/')
        self.assertRedirects(response, '/score/', fetch_redirect_response=False)

    def test_games_and_totals_pages_link_to_the_reader_when_not_dual(self):
        self.league.dual_entry_scoring = False
        self.league.save()
        user, _ = self.make_admin()
        self.client.force_login(user)

        # Admins land on the lineup page first; the button must be there too.
        response = self.client.get(f'/score/match/{self.match.pk}/lineup/')
        self.assertContains(response, 'Read a photo of the sheet')
        self._set_lineup()
        response = self.client.get(f'/score/match/{self.match.pk}/games/')
        self.assertContains(response, 'Read a photo of the sheet')
        response = self.client.get(f'/score/match/{self.match.pk}/?totals=1')
        self.assertContains(response, 'Read a photo of the sheet')

        # Captains never see it, even on the same page.
        captain, _ = self.make_captain(self.home_players[0])
        self.client.force_login(captain)
        response = self.client.get(f'/score/match/{self.match.pk}/games/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Read a photo of the sheet')

    def test_games_page_hides_reader_without_key(self):
        self.league.dual_entry_scoring = False
        self.league.save()
        user, _ = self.make_admin()
        self.client.force_login(user)
        self._set_lineup()
        with override_settings(ANTHROPIC_API_KEY=''):
            response = self.client.get(f'/score/match/{self.match.pk}/games/')
            self.assertNotContains(response, 'Read a photo of the sheet')
            response = self.client.get(f'/score/match/{self.match.pk}/lineup/')
            self.assertNotContains(response, 'Read a photo of the sheet')

    def test_resolve_page_links_to_the_reader(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        response = self.client.get(f'/score/match/{self.match.pk}/resolve/')
        self.assertContains(response, 'Read a photo of the sheet')

    def test_upload_review_and_load(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS, run_outs=['Red'])

        with mock.patch.object(scoresheet, 'extract_scoresheet', return_value=(data, FAKE_USAGE)) as extract:
            response = self._post_photo()
        self.assertEqual(extract.call_count, 1)
        kwargs = extract.call_args.kwargs
        self.assertEqual(kwargs['home_team'], 'Sharks')
        self.assertIn('Amanda', kwargs['home_roster'])

        scan = ScoresheetScan.objects.get(match=self.match)
        self.assertRedirects(response, f'/score/match/{self.match.pk}/scan/{scan.pk}/', fetch_redirect_response=False)
        self.assertEqual(scan.input_tokens, 1800)
        self.assertEqual(scan.cost_usd, Decimal('0.0152'))
        self.assertEqual(scan.issue_count, 0)

        response = self.client.get(f'/score/match/{self.match.pk}/scan/{scan.pk}/')
        self.assertContains(response, 'Everything cross-checks')
        self.assertContains(response, 'Load into the match')
        self.assertContains(response, '11&ndash;14')

        response = self.client.post(f'/score/match/{self.match.pk}/scan/{scan.pk}/', {'side': 'away'})
        self.assertRedirects(response, f'/score/match/{self.match.pk}/resolve/', fetch_redirect_response=False)
        entry = MatchEntry.objects.get(match=self.match, side='away')
        self.assertEqual(entry.status, MatchEntry.Status.SUBMITTED)
        scan.refresh_from_db()
        self.assertEqual(scan.applied_side, 'away')
        self.assertIsNotNone(scan.applied_at)

        # The referee page now offers to finalize from that side.
        response = self.client.get(f'/score/match/{self.match.pk}/resolve/')
        self.assertContains(response, 'Finalize from Jets')

    def test_review_shows_issues_and_still_allows_loading(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        away_marks = list(SAMPLE_AWAY_MARKS)
        away_marks[0] = 'WWWWW'
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, away_marks)

        with mock.patch.object(scoresheet, 'extract_scoresheet', return_value=(data, FAKE_USAGE)):
            self._post_photo()
        scan = ScoresheetScan.objects.get(match=self.match)
        response = self.client.get(f'/score/match/{self.match.pk}/scan/{scan.pk}/')
        self.assertContains(response, 'Check these before loading')
        self.assertContains(response, 'Load anyway and fix by hand')

        response = self.client.post(f'/score/match/{self.match.pk}/scan/{scan.pk}/', {'side': 'home'})
        self.assertRedirects(response, f'/score/match/{self.match.pk}/resolve/', fetch_redirect_response=False)
        entry = MatchEntry.objects.get(match=self.match, side='home')
        # One game had no winner, so it's a draft with 24 games.
        self.assertEqual(entry.status, MatchEntry.Status.DRAFT)
        self.assertEqual(entry.games.count(), 24)

    def test_reader_errors_are_shown_not_raised(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        with mock.patch.object(scoresheet, 'extract_scoresheet', side_effect=scoresheet.ScanError('nope')):
            self._post_photo()
        response = self.client.get(f'/score/match/{self.match.pk}/scan/')
        self.assertContains(response, 'nope')
        self.assertEqual(ScoresheetScan.objects.count(), 0)

    def test_attempt_limit_per_match(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)
        with mock.patch.object(scoresheet, 'extract_scoresheet', return_value=(data, FAKE_USAGE)) as extract:
            self._post_photo()
            self._post_photo()
            self._post_photo()
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(ScoresheetScan.objects.count(), 2)
        response = self.client.get(f'/score/match/{self.match.pk}/scan/')
        self.assertContains(response, 'All 2 photo attempts')

    def test_finalized_match_refuses_new_readings(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        for side in ('home', 'away'):
            MatchEntry.objects.create(match=self.match, side=side, status=MatchEntry.Status.FINALIZED)
        with mock.patch.object(scoresheet, 'extract_scoresheet') as extract:
            response = self._post_photo()
        self.assertEqual(extract.call_count, 0)
        self.assertRedirects(response, '/score/', fetch_redirect_response=False)

    def test_season_usage_total_shown(self):
        user, _ = self.make_admin()
        self.client.force_login(user)
        ScoresheetScan.objects.create(match=self.match, cost_usd=Decimal('0.010'))
        ScoresheetScan.objects.create(match=self.match, cost_usd=Decimal('0.005'))
        response = self.client.get(f'/score/match/{self.match.pk}/scan/')
        self.assertContains(response, '2 readings')
        self.assertContains(response, '$0.015')


class NonDualScanTests(EightBallScanBase):
    @override_settings(ANTHROPIC_API_KEY='sk-ant-test')
    def test_non_dual_league_loads_games_and_scores(self):
        self.league.dual_entry_scoring = False
        self.league.save()
        user, _ = self.make_admin()
        self.client.force_login(user)
        data = sheet_data(SAMPLE_HOME, SAMPLE_AWAY, SAMPLE_HOME_MARKS, SAMPLE_AWAY_MARKS)

        photo = io.BytesIO(fake_image_bytes())
        photo.name = 'sheet.jpg'
        with mock.patch.object(scoresheet, 'extract_scoresheet', return_value=(data, FAKE_USAGE)):
            self.client.post(f'/score/match/{self.match.pk}/scan/', {'photo': photo})
        scan = ScoresheetScan.objects.get(match=self.match)
        response = self.client.post(f'/score/match/{self.match.pk}/scan/{scan.pk}/')
        self.assertRedirects(response, f'/score/match/{self.match.pk}/games/', fetch_redirect_response=False)

        # Games rolled up into the match result the rest of the site reads.
        result = MatchResult.objects.get(match=self.match)
        red = PlayerMatchResult.objects.get(match_result=result, player__name='Red')
        self.assertEqual(red.wins, 4)
        self.assertEqual(result.player_results.count(), 10)
