from django.core.management.base import BaseCommand

from core.models import League
from scoring.notifications import send_score_reminders


class Command(BaseCommand):
    help = (
        'Email approved captains who have not yet entered a score for a recent '
        'match (within --days) in an active season. Intended to run from cron.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=7,
            help='Only remind about matches whose week date is within this many '
                 'days of today (default 7).',
        )
        parser.add_argument(
            '--league-id', type=int, default=None,
            help='Limit to a single league (default: all active seasons).',
        )

    def handle(self, *args, **options):
        league = None
        if options['league_id'] is not None:
            league = League.objects.filter(pk=options['league_id']).first()
            if league is None:
                self.stderr.write(self.style.ERROR('No league with that id.'))
                return
        sent = send_score_reminders(days=options['days'], league=league)
        self.stdout.write(self.style.SUCCESS(f'Sent {sent} reminder email(s).'))
