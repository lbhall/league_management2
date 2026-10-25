"""A different player taking over a one-pocket spot mid-season."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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

    def play(self, home, away, home_score, away_score, day):
        self.week_no += 1
        week = Week.objects.create(season=self.season, date=day, number=self.week_no)
        match = Match.objects.create(week=week, home_team=home, away_team=away)
        return MatchResult.objects.create(match=match, home_team_score=home_score, away_team_score=away_score)

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

        response = self.client.post(url, {'taking_over': 'Sysco', 'left': '', 'took_over_on': '2026-08-09'}, follow=True)
        self.assertRedirects(response, reverse('admin:core_team_change', args=[self.beau.pk]))
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

    def test_popup_shows_who_played(self):
        record_player_change(self.beau, 'Sysco', date(2026, 8, 9))
        with self.settings(FRONTEND_LEAGUE_ID=self.league.id):
            html = self.client.get(
                reverse('team_schedule_modal', args=[self.beau.pk]), HTTP_HOST='testserver',
            ).json()['html']
        self.assertIn('played by Beau', html)
        self.assertIn('Earlier this season this spot was played by Beau', html)
        self.assertEqual(html.count('played by Beau'), 2)  # note + one row
