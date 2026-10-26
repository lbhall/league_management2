"""Link archived player rows to current players using a name-mapping CSV.

Older archives were imported with full names ("Wayman Biles") while the
live roster uses short ones ("Wayman"), so their rows never linked and the
player popup's Past Seasons misses them. The CSV says which archived name
is which current player; every archived row with that name, in every
archive of the league, gets linked.

CSV columns: archived_name, live_name, apply, note
  - apply must be "yes" for a row to be used; anything else is skipped so
    uncertain matches can be kept in the file for later review.
  - A sub record ("Wayman (SUB)") is a separate player from the regular
    one ("Wayman"), so a mapping may never cross that line.

Dry run (reports, changes nothing):
    python manage.py link_archived_players --league "EMC Fun Pool League" \\
        --csv data/emc_archived_player_links.csv
Apply:
    python manage.py link_archived_players --league "EMC Fun Pool League" \\
        --csv data/emc_archived_player_links.csv --apply
"""
import csv
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import League, Player
from scheduling.models import ArchivedPlayer


def is_sub(name):
    return bool(re.search(r'\(\s*sub\s*\)', name, re.I))


class Command(BaseCommand):
    help = 'Link archived player rows to current players from a name-mapping CSV.'

    def add_arguments(self, parser):
        parser.add_argument('--league', required=True, help='League name')
        parser.add_argument('--csv', required=True, help='Mapping file (archived_name, live_name, apply, note)')
        parser.add_argument('--apply', action='store_true',
                            help='Make the change. Without this flag the command only reports.')

    def handle(self, *args, **options):
        try:
            league = League.objects.get(name=options['league'])
        except League.DoesNotExist:
            raise CommandError(f'League "{options["league"]}" not found.')

        try:
            with open(options['csv'], newline='', encoding='utf-8') as fh:
                rows = list(csv.DictReader(fh))
        except OSError as exc:
            raise CommandError(f'Could not read mapping file: {exc}')
        required = {'archived_name', 'live_name', 'apply'}
        if not rows or not required <= set(rows[0].keys()):
            raise CommandError('Mapping file needs the columns archived_name, live_name, apply.')

        players_by_name = {}
        for player in Player.objects.filter(league=league).order_by('id'):
            players_by_name.setdefault(player.name, []).append(player)

        planned = []   # (archived_name, player, [ArchivedPlayer...])
        skipped = []
        problems = []
        for row in rows:
            archived_name = (row['archived_name'] or '').strip()
            live_name = (row['live_name'] or '').strip()
            if not archived_name:
                continue
            if (row['apply'] or '').strip().lower() != 'yes':
                skipped.append(archived_name)
                continue
            if is_sub(archived_name) != is_sub(live_name):
                problems.append(
                    f'"{archived_name}" -> "{live_name}": a (SUB) record and a regular player are kept separate'
                )
                continue
            matches = players_by_name.get(live_name, [])
            if len(matches) != 1:
                problems.append(
                    f'"{archived_name}": live player "{live_name}" '
                    f'{"not found" if not matches else "is ambiguous (" + str(len(matches)) + " players)"}'
                )
                continue
            player = matches[0]
            archived_rows = list(
                ArchivedPlayer.objects.filter(archived_season__league=league, player_name=archived_name)
                .exclude(player=player)
                .select_related('archived_season', 'player')
            )
            if not archived_rows:
                skipped.append(f'{archived_name} (already linked or not in any archive)')
                continue
            planned.append((archived_name, player, archived_rows))

        if problems:
            raise CommandError('Fix these rows first:\n  ' + '\n  '.join(problems))

        for archived_name, player, archived_rows in planned:
            seasons = ', '.join(sorted(r.archived_season.name for r in archived_rows))
            relinks = [r for r in archived_rows if r.player_id is not None]
            note = f' (re-linking {len(relinks)} row(s) from another player)' if relinks else ''
            self.stdout.write(f'{archived_name} -> {player.name}: {len(archived_rows)} archive row(s) [{seasons}]{note}')
        if skipped:
            self.stdout.write(f'Skipped {len(skipped)} row(s) not marked apply=yes or with nothing to do.')

        total = sum(len(r) for _, _, r in planned)
        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {total} archive row(s) across {len(planned)} name(s) would be linked. '
                'Re-run with --apply to link them.'
            ))
            return

        with transaction.atomic():
            for _, player, archived_rows in planned:
                ArchivedPlayer.objects.filter(pk__in=[r.pk for r in archived_rows]).update(player=player)

        self.stdout.write(self.style.SUCCESS(f'Linked {total} archive row(s) across {len(planned)} name(s).'))
