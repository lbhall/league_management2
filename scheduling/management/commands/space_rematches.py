"""Reorder a season's weeks so teams don't play each other again too soon.

Whole weeks move: every matchup, home/away assignment and location is kept,
so venue counts and byes per week don't change. Week dates, holiday weeks,
the weeks given with --keep and any week that already has scores stay put.
The search is seeded, so a dry run and the --apply run give the same order.
Applying saves an undo point in Schedule History. The Space Out Rematches
button on Manage Schedule does the same from the browser.

Dry run (shows the new order, changes nothing):
    python manage.py space_rematches --league "EMC Fun Pool League" --keep 1 6
Apply:
    python manage.py space_rematches --league "EMC Fun Pool League" --keep 1 6 --apply
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import League, Team
from scheduling.models import Season
from scheduling.services import (
    apply_rematch_spacing,
    capture_schedule,
    plan_rematch_spacing,
    record_schedule_snapshot,
)


class Command(BaseCommand):
    help = 'Reorder whole weeks so rematches are spread out.'

    def add_arguments(self, parser):
        parser.add_argument('--league', required=True, help='League name')
        parser.add_argument('--working', action='store_true',
                            help='Use the working season instead of the live one.')
        parser.add_argument('--keep', type=int, nargs='*', default=[],
                            help='Week numbers that must not change')
        parser.add_argument('--min-gap', type=int, default=4,
                            help='Fewest weeks wanted between two meetings of the same teams (default 4)')
        parser.add_argument('--seed', type=int, default=1, help='Random seed for the search')
        parser.add_argument('--apply', action='store_true',
                            help='Make the change. Without this flag the command only reports.')

    def handle(self, *args, **options):
        try:
            league = League.objects.get(name=options['league'])
        except League.DoesNotExist:
            raise CommandError(f'League "{options["league"]}" not found.')

        status = Season.Status.WORKING if options['working'] else Season.Status.ACTIVE
        season = Season.objects.filter(league=league, status=status).first()
        if season is None:
            raise CommandError(f'League "{league.name}" has no {"working" if options["working"] else "live"} season.')

        try:
            plan = plan_rematch_spacing(
                season,
                keep_week_numbers=options['keep'],
                min_gap=options['min_gap'],
                random_seed=options['seed'],
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        names = dict(Team.objects.filter(league=league).values_list('id', 'name'))
        weeks = plan['weeks']
        min_gap = options['min_gap']
        self.stdout.write(f'{league.name}: {season.name}')
        self.stdout.write(f'Weeks that stay put: {", ".join(map(str, plan["locked"])) or "none"}')
        self._summary('Before', plan['before'], weeks, names, min_gap)
        self._summary('After', plan['after'], weeks, names, min_gap)

        if not plan['improved']:
            self.stdout.write(self.style.WARNING('No better week order found: nothing to change.'))
            return

        self.stdout.write('New order:')
        for target, source_index in zip(weeks, plan['order']):
            source = weeks[source_index]
            label = 'stays' if source.id == target.id else f'gets week {source.number}'
            matchups = ', '.join(
                f'{m.home_team.name} v {m.away_team.name}'
                for m in source.matches.select_related('home_team', 'away_team').order_by('sort_order', 'id')
            )
            self.stdout.write(f'    Week {target.number:>2} ({target.date}) {label}: {matchups}')

        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run: nothing changed. Re-run with --apply to reorder.'))
            return

        before = capture_schedule(season)
        moved = apply_rematch_spacing(plan)
        record_schedule_snapshot(season, 'Before Space Out Rematches (command)', before)
        self.stdout.write(self.style.SUCCESS(
            f'Reordered {moved} week(s). Undo it from Schedule History on the Manage Schedule page.'))

    def _summary(self, label, stats, weeks, names, min_gap):
        close = [r for r in stats['rematches'] if r[0] < min_gap]
        closest = stats['rematches'][0][0] if stats['rematches'] else None
        self.stdout.write(
            f'{label}: closest rematch {closest} week(s) apart, '
            f'{len(close)} under {min_gap}, longest home/away run {max(stats["longest_runs"].values(), default=0)}, '
            f'{stats["back_to_back_byes"]} back-to-back bye(s)')
        for gap, team_a, team_b, first, second in close:
            self.stdout.write(f'    {names[team_a]} v {names[team_b]}: weeks {weeks[first].number} and {weeks[second].number} ({gap} apart)')
