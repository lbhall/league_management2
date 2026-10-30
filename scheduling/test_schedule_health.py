from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from results.models import MatchResult
from scheduling import services
from scheduling.models import Match, ScheduleSnapshot, Season, Week
from scheduling.test_services import make_league, make_team, make_teams, make_venue


def play_weeks(season):
    return list(season.weeks.filter(number__isnull=False).order_by('date'))


def schedule_shape(season):
    """(date, home, away) for every match, ignoring ids."""
    return sorted(
        (m.week.date, m.home_team_id, m.away_team_id)
        for m in Match.objects.filter(week__season=season).select_related('week')
    )


class ScheduleFixture(TestCase):
    """Four teams playing a double round robin in which every pairing's
    return match is the very next week, with a holiday after week 3."""

    def setUp(self):
        self.league = make_league(name='Health League')
        self.venue = make_venue(self.league, max_home_teams=4)
        self.a, self.b, self.c, self.d = make_teams(self.league, self.venue, 4)
        self.season = Season.objects.create(league=self.league, name='S1', status=Season.Status.ACTIVE)
        a, b, c, d = self.a, self.b, self.c, self.d
        self.build([
            [(a, b), (c, d)], [(b, a), (d, c)],
            [(a, c), (b, d)], [(c, a), (d, b)],
            [(a, d), (b, c)], [(d, a), (c, b)],
        ])

    def build(self, rounds, holiday_before=4):
        week_date = date(2026, 1, 5)
        for number, pairs in enumerate(rounds, start=1):
            if number == holiday_before:
                Week.objects.create(season=self.season, date=week_date, notes='Holiday')
                week_date += timedelta(days=7)
            week = Week.objects.create(season=self.season, date=week_date, number=number)
            for sort_order, (home, away) in enumerate(pairs, start=1):
                Match.objects.create(week=week, home_team=home, away_team=away,
                                     location=self.venue.name, sort_order=sort_order)
            week_date += timedelta(days=7)


class ScheduleHealthTests(ScheduleFixture):
    def test_flags_rematches_closer_than_the_minimum_gap(self):
        health = services.schedule_health(self.season)
        self.assertEqual(len(health['close_rematches']), 6)
        self.assertEqual({r['gap'] for r in health['close_rematches']}, {1})
        first = health['close_rematches'][0]
        self.assertEqual((first['first_week'].number, first['second_week'].number), (1, 2))

    def test_a_well_spread_schedule_has_no_issues(self):
        Match.objects.filter(week__season=self.season).delete()
        Week.objects.filter(season=self.season).delete()
        a, b, c, d = self.a, self.b, self.c, self.d
        self.build([
            [(a, b), (c, d)], [(a, c), (b, d)], [(a, d), (b, c)],
            [(b, a), (d, c)], [(c, a), (d, b)], [(d, a), (c, b)],
        ])
        health = services.schedule_health(self.season, min_gap=3)
        self.assertEqual(health['issue_count'], 0)

    def test_flags_long_runs_byes_and_relocated_matches(self):
        Match.objects.filter(week__season=self.season).delete()
        Week.objects.filter(season=self.season).delete()
        e = make_team(self.league, self.venue, 'Team 5')
        a, b, c, d = self.a, self.b, self.c, self.d
        self.build([[(a, b)], [(a, c)], [(a, d)], [(a, e)]], holiday_before=None)
        match = Match.objects.get(week__season=self.season, away_team=e)
        match.location = 'Somewhere Else'
        match.save()

        health = services.schedule_health(self.season)
        run = health['long_runs'][0]
        self.assertEqual((run['team'], run['side'], run['length']), (a.name, 'home', 4))
        self.assertEqual((run['first_week'].number, run['last_week'].number), (1, 4))
        self.assertTrue(health['back_to_back_byes'])
        self.assertEqual(health['uneven_byes'][-1], (a.name, 0))
        self.assertEqual([item['match'] for item in health['relocated']], [match])

    def test_matches_against_the_bye_team_are_byes(self):
        bye = make_team(self.league, self.venue, 'BYE')
        week = play_weeks(self.season)[0]
        Match.objects.create(week=week, home_team=bye, away_team=self.a, location='Elsewhere')
        Match.objects.create(week=play_weeks(self.season)[1], home_team=self.a, away_team=bye)
        health = services.schedule_health(self.season)
        self.assertFalse(health['relocated'])
        self.assertNotIn('BYE', {name for r in health['close_rematches'] for name in r['teams']})


class MirrorSpacingTests(TestCase):
    def test_mirroring_spreads_the_return_matches(self):
        league = make_league(name='Mirror League')
        venue = make_venue(league, max_home_teams=4)
        a, b, c, d = make_teams(league, venue, 4)
        season = Season.objects.create(league=league, name='S1', status=Season.Status.WORKING)
        for number, pairs in enumerate([[(a, b), (c, d)], [(a, c), (b, d)], [(a, d), (b, c)]], start=1):
            week = Week.objects.create(season=season, date=date(2026, 1, 5) + timedelta(weeks=number - 1), number=number)
            for home, away in pairs:
                Match.objects.create(week=week, home_team=home, away_team=away, location=venue.name)

        services.create_mirrored_season_schedule(season)

        self.assertEqual(Match.objects.filter(week__season=season).count(), 12)
        self.assertEqual(services.schedule_health(season, min_gap=3)['close_rematches'], [])


class WeekOrderFromIdsTests(ScheduleFixture):
    def test_refuses_a_stale_order(self):
        ids = [week.id for week in play_weeks(self.season)]
        with self.assertRaises(ValueError):
            services.week_order_from_ids(self.season, ids[:-1])

    def test_refuses_moving_a_locked_week(self):
        ids = [week.id for week in play_weeks(self.season)]
        ids[0], ids[2] = ids[2], ids[0]
        with self.assertRaises(ValueError):
            services.week_order_from_ids(self.season, ids, keep_week_numbers=[1])
        MatchResult.objects.create(match=play_weeks(self.season)[2].matches.first(),
                                   home_team_score=3, away_team_score=1)
        with self.assertRaises(ValueError):
            services.week_order_from_ids(self.season, ids)


class ScheduleSnapshotTests(ScheduleFixture):
    def reorder(self):
        before = services.capture_schedule(self.season)
        plan = services.plan_rematch_spacing(self.season, min_gap=3)
        services.apply_rematch_spacing(plan)
        return services.record_schedule_snapshot(self.season, 'Before Space Out Rematches', before)

    def test_nothing_is_kept_when_the_schedule_did_not_change(self):
        before = services.capture_schedule(self.season)
        self.assertIsNone(services.record_schedule_snapshot(self.season, 'Before nothing', before))
        self.assertFalse(ScheduleSnapshot.objects.exists())

    def test_restore_undoes_a_reorder_and_can_itself_be_undone(self):
        original = schedule_shape(self.season)
        snapshot = self.reorder()
        reordered = schedule_shape(self.season)
        self.assertNotEqual(reordered, original)

        plan = services.plan_schedule_restore(snapshot)
        self.assertTrue(plan['allowed'])
        services.restore_schedule_snapshot(snapshot)
        self.assertEqual(schedule_shape(self.season), original)

        undo_of_undo = self.season.schedule_snapshots.first()
        self.assertEqual(undo_of_undo.label, 'Before undoing Space Out Rematches')
        services.restore_schedule_snapshot(undo_of_undo)
        self.assertEqual(schedule_shape(self.season), reordered)

    def test_restore_puts_back_deleted_weeks_and_matches(self):
        original = schedule_shape(self.season)
        before = services.capture_schedule(self.season)
        last = play_weeks(self.season)[-1]
        last.matches.all().delete()
        last.delete()
        snapshot = services.record_schedule_snapshot(self.season, 'Before deleting week 6', before)

        plan = services.plan_schedule_restore(snapshot)
        self.assertEqual((plan['weeks_created'], plan['matches_created']), (1, 2))
        services.restore_schedule_snapshot(snapshot)
        self.assertEqual(schedule_shape(self.season), original)
        self.assertEqual([w.number for w in play_weeks(self.season)], [1, 2, 3, 4, 5, 6])
        self.assertTrue(self.season.weeks.filter(number__isnull=True, notes='Holiday').exists())

    def test_restore_refused_when_a_scored_match_would_move(self):
        snapshot = self.reorder()
        moved = next(
            m for m in Match.objects.filter(week__season=self.season)
            if (m.week.date, m.home_team_id, m.away_team_id) not in {
                (date.fromisoformat(w['date']), i['home_team_id'], i['away_team_id'])
                for i in snapshot.data['matches'] for w in snapshot.data['weeks'] if w['id'] == i['week_id']
            }
        )
        MatchResult.objects.create(match=moved, home_team_score=3, away_team_score=1)
        plan = services.plan_schedule_restore(snapshot)
        self.assertFalse(plan['allowed'])
        self.assertIn('already have scores', plan['reason'])
        with self.assertRaises(ValueError):
            services.restore_schedule_snapshot(snapshot)

    def test_only_the_newest_snapshots_are_kept(self):
        week = play_weeks(self.season)[0]
        for i in range(services.SCHEDULE_SNAPSHOTS_KEPT + 3):
            before = services.capture_schedule(self.season)
            week.notes = f'note {i}'
            week.save()
            services.record_schedule_snapshot(self.season, f'Before note {i}', before)
        self.assertEqual(self.season.schedule_snapshots.count(), services.SCHEDULE_SNAPSHOTS_KEPT)
        self.assertEqual(self.season.schedule_snapshots.first().label, f'Before note {services.SCHEDULE_SNAPSHOTS_KEPT + 2}')


class ScheduleToolsAdminTests(ScheduleFixture):
    def setUp(self):
        super().setUp()
        get_user_model().objects.create_superuser(username='admin', password='password123', email='a@example.com')
        self.client.login(username='admin', password='password123')
        self.manage_url = reverse('admin:scheduling_season_manage_schedule', args=[self.season.pk])
        self.space_url = reverse('admin:scheduling_season_space_rematches', args=[self.season.pk])

    def test_manage_schedule_shows_the_health_check_and_tools(self):
        html = self.client.get(self.manage_url).content.decode()
        self.assertIn('data-schedule-health', html)
        self.assertIn('data-health-rematches', html)
        self.assertIn('data-space-rematches', html)
        self.assertIn('data-schedule-history-link', html)
        self.assertNotIn('data-undo-last', html)

    def test_preview_then_apply_spreads_rematches_and_offers_undo(self):
        response = self.client.get(self.space_url, {'preview': '1', 'min_gap': '3'})
        order = response.context['preview']['order']
        self.assertTrue(response.context['preview']['improved'])
        self.assertGreater(services.schedule_health(self.season, min_gap=3)['issue_count'], 0)

        response = self.client.post(self.space_url, {'order': order, 'min_gap': '3'})
        self.assertRedirects(response, self.manage_url)
        self.assertEqual(services.schedule_health(self.season, min_gap=3)['close_rematches'], [])
        self.assertEqual(self.season.schedule_snapshots.first().label, 'Before Space Out Rematches')
        self.assertIn('data-undo-last', self.client.get(self.manage_url).content.decode())

    def test_apply_refuses_a_stale_or_locked_order(self):
        original = schedule_shape(self.season)
        ids = [week.id for week in play_weeks(self.season)]
        response = self.client.post(self.space_url, {'order': ','.join(map(str, ids[:-1]))})
        self.assertRedirects(response, self.space_url)
        ids[0], ids[1] = ids[1], ids[0]
        self.client.post(self.space_url, {'order': ','.join(map(str, ids)), 'keep': '1'})
        self.assertEqual(schedule_shape(self.season), original)
        self.assertFalse(ScheduleSnapshot.objects.exists())

    def test_history_and_restore_pages(self):
        original = schedule_shape(self.season)
        week2 = play_weeks(self.season)[1]
        self.client.post(reverse('admin:scheduling_season_move_week_up', args=[self.season.pk, week2.pk]))
        snapshot = self.season.schedule_snapshots.get()
        self.assertEqual(snapshot.label, 'Before moving week 2 up')

        history = self.client.get(reverse('admin:scheduling_season_schedule_history', args=[self.season.pk]))
        self.assertContains(history, 'Before moving week 2 up')

        restore_url = reverse('admin:scheduling_season_restore_snapshot', args=[self.season.pk, snapshot.pk])
        self.assertContains(self.client.get(restore_url), 'data-restore-changes')
        self.assertNotEqual(schedule_shape(self.season), original)
        response = self.client.post(restore_url, {'confirm': 'yes'})
        self.assertRedirects(response, self.manage_url)
        self.assertEqual(schedule_shape(self.season), original)

    def test_a_snapshot_from_another_season_is_not_found(self):
        other = Season.objects.create(league=self.league, name='S2', status=Season.Status.WORKING)
        snapshot = ScheduleSnapshot.objects.create(season=other, label='Before x', data={'weeks': [], 'matches': []})
        url = reverse('admin:scheduling_season_restore_snapshot', args=[self.season.pk, snapshot.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_archived_season_cannot_be_spaced(self):
        self.season.status = Season.Status.ARCHIVED
        self.season.save()
        self.assertRedirects(self.client.get(self.space_url), self.manage_url)
