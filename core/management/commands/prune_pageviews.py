from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import PageView


class Command(BaseCommand):
    help = 'Delete page view rows older than N days (default 400) to keep the table small.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=400)

    def handle(self, *args, **options):
        cutoff = timezone.localdate() - timedelta(days=options['days'])
        deleted, _ = PageView.objects.filter(day__lt=cutoff).delete()
        self.stdout.write(f'Deleted {deleted} page view(s) before {cutoff}.')
