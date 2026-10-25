"""Swap the matches of two weeks in a league's working season.

The week dates, numbers and holiday notes stay where they are; only the
matches move, so every matchup, home/away assignment and location is kept.
Use it to fix things like a team drawing back-to-back byes.

Dry run (shows what would move, changes nothing):
    python manage.py swap_weeks --league "EMC Fun Pool League" 10 14
Apply:
    python manage.py swap_weeks --league "EMC Fun Pool League" 10 14 --apply
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import League
from scheduling.models import Match, Season


class Command(BaseCommand):
    help = 'Swap the matches of two numbered weeks in a league\'s working season.'

    def add_arguments(self, parser):
        parser.add_argument('--league', required=True, help='League name')
        parser.add_argument('week_a', type=int, help='First week number')
        parser.add_argument('week_b', type=int, help='Second week number')
        parser.add_argument('--apply', action='store_true',
                            help='Make the change. Without this flag the command only reports.')

    def handle(self, *args, **options):
        try:
            league = League.objects.get(name=options['league'])
        except League.DoesNotExist:
            raise CommandError(f'League "{options["league"]}" not found.')

        season = Season.objects.filter(league=league, status=Season.Status.WORKING).first()
        if season is None:
            raise CommandError(f'League "{league.name}" has no working season.')

        a, b = options['week_a'], options['week_b']
        if a == b:
            raise CommandError('Pick two different weeks.')
        weeks = {}
        for number in (a, b):
            week = season.weeks.filter(number=number).first()
            if week is None:
                raise CommandError(f'Week {number} not found in "{season.name}".')
            weeks[number] = week

        matches_a = list(Match.objects.filter(week=weeks[a]).select_related('home_team', 'away_team'))
        matches_b = list(Match.objects.filter(week=weeks[b]).select_related('home_team', 'away_team'))
        played = [m for m in matches_a + matches_b if hasattr(m, 'result')]
        if played:
            raise CommandError('Refusing to swap: a match in one of these weeks already has a result.')

        for number, matches in ((a, matches_a), (b, matches_b)):
            other = b if number == a else a
            self.stdout.write(f'Week {number} ({weeks[number].date}) -> week {other} ({weeks[other].date}):')
            for m in matches:
                self.stdout.write(f'    {m.home_team.name} vs {m.away_team.name} @ {m.location}')

        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing changed. Re-run with --apply to swap.'))
            return

        with transaction.atomic():
            Match.objects.filter(pk__in=[m.pk for m in matches_a]).update(week=weeks[b])
            Match.objects.filter(pk__in=[m.pk for m in matches_b]).update(week=weeks[a])

        self.stdout.write(self.style.SUCCESS(
            f'Swapped {len(matches_a)} match(es) from week {a} with {len(matches_b)} from week {b}.'))
