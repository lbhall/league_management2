"""A different player taking over a one-pocket spot mid-season."""
from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import League, Player, Team, Venue
from core.player_change import PlayerChangeError, record_player_change
from core.rank_review import build_rank_review
from results.models import MatchResult
from scheduling import services
from scheduling.models import Match, Season, Week


class PlayerChangeBase(TestCase):
    def setUp(self):
        self.league = League.objects.create(
            name='Bogies', team_size=1, results_type=League.ResultsType.ONE_POCKET,
            day_of_week=League.DayOfWeek.SUNDAY,
        )
        self.venue = Venue.objects.create(
            league=self.league, name='Bogies', phone='', address='', number_of_tables=9,
            max_home_teams=9, min_home_teams=1,
        )
        self.season = Season.objects.create(league=self.league, name='Mid 2026', status=Season.Status.ACTIVE)
        # Team.save() auto-creates a Player of the same name for one-pocket.
        self.beau = Team.objects.create(league=self.league, venue=self.venue, name='Beau', team_rank=2)
        self.lupe = Team.objects.create(league=self.league, venue=self.venue, name='Lupe', team_rank=2)
        self.abel = Team.objects.create(league=self.league, venue=self.venue, name='Abel', team_rank=5)
        self.week_no = 0

    def play(self, home, away, home_score, away_score, day, entered=None):
        """Record a result; the score counts as entered on ``entered`` (defaults to the week date)."""
        self.week_no += 1
        week = Week.objects.create(season=self.season, date=day, number=self.week_no)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        result = MatchResult.objects.create(match=match, home_team_score=home_score, away_team_score=away_score)
        entered_on = entered or day
        stamp = timezone.make_aware(datetime.combine(entered_on, time(21, 0)))
        MatchResult.objects.filter(pk=result.pk).update(created_at=stamp)
        result.refresh_from_db()
        return result

    def player(self, name):
        return Player.objects.get(league=self.league, name=name)


class AttributionOnSaveTests(PlayerChangeBase):
    def test_result_is_credited_to_the_spot_holders(self):
        result = self.play(self.beau, self.lupe, 3, 1, date(2026, 7, 5))
        self.assertEqual(result.home_player, self.player('Beau'))
        self.assertEqual(result.away_player, self.player('Lupe'))

    def test_side_player_falls_back_to_current_holder(self):
        result = self.play(self.beau, self.lupe, 3, 1, date(2026, 7, 5))
        MatchResult.objects.filter(pk=result.pk).update(home_player=None)
        result.refresh_from_db()
        self.assertEqual(result.side_player(self.beau), self.player('Beau'))

    def test_non_one_pocket_results_are_not_touched(self):
        emc = League.objects.create(
            name='EMC', team_size=5, results_type=League.ResultsType.EIGHT_BALL, day_of_week=League.DayOfWeek.MONDAY,
        )
        venue = Venue.objects.create(
            league=emc, name='Cue Club', phone='', address='', number_of_tables=3, max_home_teams=2, min_home_teams=1,
        )
        a = Team.objects.create(league=emc, venue=venue, name='A')
        b = Team.objects.create(league=emc, venue=venue, name='B')
        season = Season.objects.create(league=emc, name='S', status=Season.Status.ACTIVE)
        week = Week.objects.create(season=season, date=date(2026, 7, 6), number=1)
        result = MatchResult.objects.create(match=Match.objects.create(week=week, home_team=a, away_team=b))
        self.assertIsNone(result.home_player)


class DuplicatePlayerNameTests(PlayerChangeBase):
    """Hand renames used to leave a second, empty player with the new name."""

    def test_team_save_tolerates_duplicate_player_names(self):
        stray = Player.objects.create(league=self.league, name='Beau')  # empty duplicate
        self.beau.team_rank = 3
        self.beau.save()  # used to raise MultipleObjectsReturned
        self.beau.refresh_from_db()
        self.assertEqual(self.beau.captain, self.player_on_spot('Beau'))
        self.assertNotEqual(self.beau.captain_id, stray.id)

    def test_team_save_prefers_the_player_on_the_spot(self):
        on_spot = self.player_on_spot('Beau')
        Player.objects.create(league=self.league, name='beau')  # older-looking duplicate elsewhere
        self.beau.save()
        self.beau.refresh_from_db()
        self.assertEqual(self.beau.captain_id, on_spot.id)

    def test_record_change_removes_the_stray_duplicate(self):
        # Spot already renamed by hand: a stray 'Sysco' with no matches exists next to the real one.
        real = self.player_on_spot('Beau')
        real.name = 'Sysco'
        real.save()
        self.beau.name = 'Sysco'
        self.beau.save()
        stray = Player.objects.create(league=self.league, name='Sysco')
        self.play(self.beau, self.lupe, 3, 1, date(2026, 7, 5))

        old, new, _ = record_player_change(self.beau, 'Sysco', date(2026, 8, 9), left='Beau')

        self.assertEqual(new.pk, real.pk)
        self.assertFalse(Player.objects.filter(pk=stray.pk).exists())
        self.assertEqual(Player.objects.filter(league=self.league, name__iexact='Sysco').count(), 1)
        # And the team saves cleanly afterwards.
        self.beau.team_rank = 3
        self.beau.save()

    def test_record_change_keeps_duplicates_that_have_history(self):
        real = self.player_on_spot('Beau')
        real.name = 'Sysco'
        real.save()
        self.beau.name = 'Sysco'
        self.beau.save()
        other = Player.objects.create(league=self.league, name='Sysco')
        # 'other' has a result credited to them, so they must survive.
        result = self.play(self.beau, self.lupe, 3, 1, date(2026, 7, 5))
        MatchResult.objects.filter(pk=result.pk).update(home_player=other)

        record_player_change(self.beau, 'Sysco', date(2026, 8, 9), left='Beau')
        self.assertTrue(Player.objects.filter(pk=other.pk).exists())

    def test_record_change_keeps_duplicates_with_a_scoring_login(self):
        from django.contrib.auth import get_user_model
        from scoring.models import ScoringProfile
        real = self.player_on_spot('Beau')
        real.name = 'Sysco'
        real.save()
        self.beau.name = 'Sysco'
        self.beau.save()
        other = Player.objects.create(league=self.league, name='Sysco')
        user = get_user_model().objects.create_user(username='sysco@example.com', password='pw')
        ScoringProfile.objects.create(user=user, league=self.league, player=other, role=ScoringProfile.Role.CAPTAIN)

        record_player_change(self.beau, 'Sysco', date(2026, 8, 9), left='Beau')
        self.assertTrue(Player.objects.filter(pk=other.pk).exists())
        self.assertTrue(ScoringProfile.objects.filter(player=other).exists())

    def player_on_spot(self, team_name):
        return Player.objects.filter(team=Team.objects.get(league=self.league, name=team_name)).order_by('id').first()


class StoredRankTests(PlayerChangeBase):
    def test_ranks_are_stored_when_the_result_is_saved(self):
        result = self.play(self.beau, self.abel, 1, 3, date(2026, 7, 5))
        self.assertEqual((result.home_rank, result.away_rank), (2, 5))
        self.assertEqual(result.side_rank(self.beau), 2)
        self.assertEqual(result.side_rank(self.abel), 5)

    def test_rerank_does_not_relabel_earlier_results(self):
        result = self.play(self.beau, self.abel, 1, 3, date(2026, 7, 5))
        self.beau.team_rank = 4
        self.beau.save()
        result.refresh_from_db()
        self.assertEqual(result.side_rank(self.beau), 2)
        # Rank review judges the loss as "getting weight" (2 vs 5), not even (4 vs 5 is still getting) —
        # make the point with a rank that would flip the category.
        self.abel.team_rank = 3
        self.abel.save()
        rows = {r['name']: r for r in build_rank_review(self.league, self.season)}
        self.assertEqual(rows['Beau']['splits']['getting']['matches_lost'], 1)
        self.assertEqual(rows['Beau']['splits']['giving']['matches_lost'], 0)
        self.assertEqual(rows['Beau']['rank'], 4)  # the row shows the current rank

    def test_side_rank_falls_back_to_current_when_unstored(self):
        result = self.play(self.beau, self.abel, 1, 3, date(2026, 7, 5))
        MatchResult.objects.filter(pk=result.pk).update(home_rank=None)
        result.refresh_from_db()
        self.assertEqual(result.side_rank(self.beau), 2)

    def test_archive_uses_the_stored_ranks(self):
        self.play(self.beau, self.abel, 1, 3, date(2026, 7, 5))
        self.beau.team_rank = 5
        self.beau.save()
        archived = services.archive_season(self.season)
        match = archived.matches.get()
        self.assertEqual((match.home_team_rank, match.away_team_rank), (2, 5))
        self.assertEqual(match.race_label, '5/11')  # rank 2 vs rank 5

    def test_player_change_with_new_rank(self):
        r1 = self.play(self.beau, self.abel, 1, 3, date(2026, 7, 5))
        r2 = self.play(self.beau, self.lupe, 3, 1, date(2026, 8, 9))
        record_player_change(self.beau, 'Sysco', date(2026, 8, 9), new_rank=3)
        self.beau.refresh_from_db()
        self.assertEqual(self.beau.team_rank, 3)
        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertEqual(r1.home_rank, 2)   # Beau's match keeps rank 2
        self.assertEqual(r2.home_rank, 3)   # Sysco's match is at the new rank

    def test_player_change_corrects_the_departing_players_rank(self):
        # The spot was renamed by hand AND reranked (Beau was a 4, Sysco is a 2) before
        # ranks were stored, so the backfill guessed 2 for everything.
        r1 = self.play(self.beau, self.abel, 1, 3, date(2026, 7, 5))
        r2 = self.play(self.beau, self.lupe, 3, 1, date(2026, 8, 9))
        real = Player.objects.get(league=self.league, name='Beau')
        real.name = 'Sysco'
        real.save()
        self.beau.name = 'Sysco'
        self.beau.save()

        record_player_change(self.beau, 'Sysco', date(2026, 8, 9), left='Beau', old_rank=4)

        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertEqual(r1.home_rank, 4)   # Beau played this one as a 4
        self.assertEqual(r1.home_player.name, 'Beau')
        self.assertEqual(r2.home_rank, 2)   # Sysco's match stays at the spot's rank
        self.assertEqual(r2.home_player.name, 'Sysco')
        # Opponents' stored ranks are untouched.
        self.assertEqual(r1.away_rank, 5)


class RecordPlayerChangeTests(PlayerChangeBase):
    def setUp(self):
        super().setUp()
        # Beau plays weeks 1-2, then Sysco takes the spot from Aug 9.
        self.r1 = self.play(self.beau, self.lupe, 3, 1, date(2026, 7, 5))
        self.r2 = self.play(self.abel, self.beau, 3, 2, date(2026, 7, 12))
        self.r3 = self.play(self.beau, self.abel, 0, 3, date(2026, 8, 9))
        self.r4 = self.play(self.lupe, self.beau, 3, 2, date(2026, 8, 16))

    def test_takeover_renames_spot_and_splits_credit(self):
        old, new, reassigned = record_player_change(self.beau, 'Sysco', date(2026, 8, 9))

        self.beau.refresh_from_db()
        self.assertEqual(self.beau.name, 'Sysco')
        self.assertEqual(old.name, 'Beau')
        self.assertEqual(new.name, 'Sysco')
        self.assertIsNone(Player.objects.get(pk=old.pk).team)
        self.assertEqual(Player.objects.get(pk=new.pk).team, self.beau)
        self.assertEqual(self.beau.captain, new)
        self.assertEqual(reassigned, 2)

        for r in (self.r1, self.r2, self.r3, self.r4):
            r.refresh_from_db()
        self.assertEqual(self.r1.home_player, old)
        self.assertEqual(self.r2.away_player, old)
        self.assertEqual(self.r3.home_player, new)
        self.assertEqual(self.r4.away_player, new)
        # The opponents' credit is untouched.
        self.assertEqual(self.r1.away_player, self.player('Lupe'))

    def test_spot_already_renamed_by_hand(self):
        # The admin typed Sysco over Beau earlier; all four results are credited to Sysco.
        beau_player = self.player('Beau')
        beau_player.name = 'Sysco'
        beau_player.save()
        self.beau.name = 'Sysco'
        self.beau.save()

        old, new, reassigned = record_player_change(self.beau, 'Sysco', date(2026, 8, 9), left='Beau')

        self.assertEqual(new.pk, beau_player.pk)
        self.assertEqual(old.name, 'Beau')
        self.assertIsNone(old.team)
        self.assertEqual(reassigned, 2)
        self.r1.refresh_from_db()
        self.r3.refresh_from_db()
        self.assertEqual(self.r1.home_player, old)
        self.assertEqual(self.r3.home_player, new)

    def test_renamed_spot_needs_the_departing_name(self):
        self.beau.name = 'Sysco'
        self.beau.save()
        with self.assertRaises(PlayerChangeError):
            record_player_change(self.beau, 'Sysco', date(2026, 8, 9))

    def test_refuses_non_one_pocket_and_blank_names(self):
        with self.assertRaises(PlayerChangeError):
            record_player_change(self.beau, '', date(2026, 8, 9))
        self.league.results_type = League.ResultsType.EIGHT_BALL
        self.league.save()
        with self.assertRaises(PlayerChangeError):
            record_player_change(self.beau, 'Sysco', date(2026, 8, 9))

    def test_goes_by_entered_date_not_scheduled_week(self):
        # A week-1 makeup that Sysco actually played and entered on Aug 20.
        late = self.play(self.beau, self.lupe, 3, 2, date(2026, 7, 19), entered=date(2026, 8, 20))
        old, new, _ = record_player_change(self.beau, 'Sysco', date(2026, 8, 9))
        late.refresh_from_db()
        self.assertEqual(late.home_player, new)
        self.r1.refresh_from_db()
        self.assertEqual(self.r1.home_player, old)

    def test_archived_seasons_are_left_alone(self):
        old_season = Season.objects.create(league=self.league, name='Early', status=Season.Status.ARCHIVED)
        week = Week.objects.create(season=old_season, date=date(2026, 1, 4), number=1)
        archived = MatchResult.objects.create(
            match=Match.objects.create(week=week, home_team=self.beau, away_team=self.lupe),
            home_team_score=3, away_team_score=0,
        )
        record_player_change(self.beau, 'Sysco', date(2026, 8, 9))
        archived.refresh_from_db()
        self.assertEqual(archived.home_player.name, 'Beau')

    def test_rank_review_splits_the_two_players(self):
        record_player_change(self.beau, 'Sysco', date(2026, 8, 9))
        rows = {r['name']: r for r in build_rank_review(self.league, self.season)}
        self.assertEqual((rows['Beau']['matches_won'], rows['Beau']['matches_lost']), (1, 1))
        self.assertFalse(rows['Beau']['active'])
        self.assertEqual(rows['Beau']['rank'], 2)
        self.assertEqual((rows['Sysco']['matches_won'], rows['Sysco']['matches_lost']), (0, 2))
        self.assertTrue(rows['Sysco']['active'])
        # Lupe's row is unaffected and still keyed by the current holder.
        self.assertEqual(rows['Lupe']['matches_won'], 1)

    def test_archive_credits_the_actual_players(self):
        record_player_change(self.beau, 'Sysco', date(2026, 8, 9))
        archived = services.archive_season(self.season)
        beau_lines = archived.matches.filter(player_results__player__name='Beau').count()
        sysco_lines = archived.matches.filter(player_results__player__name='Sysco').count()
        self.assertEqual((beau_lines, sysco_lines), (2, 2))
        self.assertEqual(archived.players.get(player_name='Beau').games_won, 5)
        self.assertEqual(archived.players.get(player_name='Sysco').games_won, 2)


class PlayerChangeViewsTests(PlayerChangeBase):
    def setUp(self):
        super().setUp()
        self.r1 = self.play(self.beau, self.lupe, 3, 1, date(2026, 7, 5))
        self.r2 = self.play(self.beau, self.abel, 0, 3, date(2026, 8, 9))
        User = get_user_model()
        self.root = User.objects.create_superuser(username='root', password='pw', email='r@example.com')
        self.client.force_login(self.root)

    def test_team_page_offers_the_action_for_one_pocket_only(self):
        response = self.client.get(reverse('admin:core_team_change', args=[self.beau.pk]))
        self.assertContains(response, 'Record a player change')
        emc = League.objects.create(
            name='EMC', team_size=5, results_type=League.ResultsType.EIGHT_BALL, day_of_week=League.DayOfWeek.MONDAY,
        )
        venue = Venue.objects.create(
            league=emc, name='Cue Club', phone='', address='', number_of_tables=3, max_home_teams=2, min_home_teams=1,
        )
        team = Team.objects.create(league=emc, venue=venue, name='Bus Boys')
        response = self.client.get(reverse('admin:core_team_change', args=[team.pk]))
        self.assertNotContains(response, 'Record a player change')

    def test_form_round_trip(self):
        url = reverse('admin:core_team_player_change', args=[self.beau.pk])
        response = self.client.get(url)
        self.assertContains(response, 'Currently in this spot: <strong>Beau</strong>', html=False)

        response = self.client.post(url, {'taking_over': 'Sysco', 'left': '', 'took_over_on': '2026-08-09', 'new_rank': '3'}, follow=True)
        self.assertRedirects(response, reverse('admin:core_team_change', args=[self.beau.pk]))
        self.beau.refresh_from_db()
        self.assertEqual(self.beau.team_rank, 3)
        self.assertContains(response, 'Recorded Beau → Sysco from 2026-08-09; 1 match result(s) re-credited.')
        self.beau.refresh_from_db()
        self.assertEqual(self.beau.name, 'Sysco')
        self.r1.refresh_from_db()
        self.r2.refresh_from_db()
        self.assertEqual(self.r1.home_player.name, 'Beau')
        self.assertEqual(self.r2.home_player.name, 'Sysco')

    def test_errors_are_shown_on_the_form(self):
        url = reverse('admin:core_team_player_change', args=[self.beau.pk])
        response = self.client.post(url, {'taking_over': 'Sysco', 'left': '', 'took_over_on': 'not-a-date'})
        self.assertContains(response, 'Enter the takeover date')
        response = self.client.post(url, {'taking_over': 'Beau', 'left': '', 'took_over_on': '2026-08-09'})
        self.assertContains(response, 'Enter the name of the player who left')
        response = self.client.post(url, {'taking_over': 'Sysco', 'left': '', 'took_over_on': '2026-08-09', 'new_rank': '9'})
        self.assertContains(response, 'Ranks must be numbers from 1 to 5')
        response = self.client.post(url, {'taking_over': 'Sysco', 'left': '', 'took_over_on': '2026-08-09', 'old_rank': 'x'})
        self.assertContains(response, 'Ranks must be numbers from 1 to 5')

    def test_popup_shows_who_played(self):
        record_player_change(self.beau, 'Sysco', date(2026, 8, 9))
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(
                reverse('team_schedule_modal', args=[self.beau.pk]), HTTP_HOST='testserver',
            ).json()['html']
        self.assertIn('played by Beau', html)
        self.assertIn('Earlier this season this spot was played by Beau', html)
        self.assertEqual(html.count('played by Beau'), 2)  # note + one row
