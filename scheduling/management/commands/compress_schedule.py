"""Repack a league's working season into the fewest possible weeks.

Matchups, home/away, and locations are untouched; only the week each match is
played in changes. Weeks left empty at the end of the season are deleted.

Run: python manage.py compress_schedule --league "EMC Fun Pool League"
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import League
from scheduling.models import Season
from scheduling.services import pack_season_schedule


class Command(BaseCommand):
    help = 'Repack the working season of a league into the fewest possible weeks.'

    def add_arguments(self, parser):
        parser.add_argument('--league', required=True, help='League name')
        parser.add_argument('--seed', type=int, default=None,
                            help='Random seed for reproducible packing')
        parser.add_argument('--attempts', type=int, default=20000,
                            help='Search budget before falling back to greedy compression')

    def handle(self, *args, **options):
        try:
            league = League.objects.get(name=options['league'])
        except League.DoesNotExist:
            raise CommandError(f'League "{options["league"]}" not found.')

        season = Season.objects.filter(league=league, status=Season.Status.WORKING).first()
        if season is None:
            raise CommandError(f'League "{league.name}" has no working season.')

        before = list(
            season.weeks.filter(number__isnull=False).order_by('date')
        )
        before_sizes = [week.matches.count() for week in before]

        result = pack_season_schedule(
            season, random_seed=options['seed'], attempts=options['attempts'])

        after_sizes = [
            week.matches.count()
            for week in season.weeks.filter(number__isnull=False).order_by('date')
        ]
        self.stdout.write(f'before: {len(before_sizes)} weeks {before_sizes}')
        self.stdout.write(f'after:  {len(after_sizes)} weeks {after_sizes}')
        label = 'optimal packing' if result['optimal'] else 'greedy compression (no optimal packing found)'
        self.stdout.write(self.style.SUCCESS(
            f"{label}: moved {result['moved']} match(es), "
            f"deleted {result['deleted_weeks']} empty week(s)."))
