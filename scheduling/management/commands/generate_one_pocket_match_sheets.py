from django.core.management.base import BaseCommand, CommandError
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from core.models import League
from core.views import get_one_pocket_race_label
from scheduling.models import Season, Match


class Command(BaseCommand):
    help = (
        'Generate a PDF with one match sheet per One Pocket match for an entire season, '
        'with Player 1, Player 2, and Race filled in.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--league',
            help='League name. Defaults to the only One Pocket league, if there is just one.',
        )
        parser.add_argument(
            '--season',
            help='Season name. Defaults to the active season for the league.',
        )
        parser.add_argument(
            '--output',
            default='one_pocket_match_sheets.pdf',
            help='Output PDF path (default: one_pocket_match_sheets.pdf).',
        )

    def handle(self, *args, **options):
        league = self._resolve_league(options['league'])
        season = self._resolve_season(league, options['season'])

        matches = list(
            Match.objects.filter(week__season=season)
            .select_related('home_team', 'away_team', 'week')
            .order_by('week__date', 'sort_order', 'id')
        )
        if not matches:
            raise CommandError(f'No matches scheduled for "{season.name}".')

        self._render_pdf(matches, options['output'])

        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(matches)} match sheet(s) to {options["output"]} for "{season.name}".'
        ))

    def _resolve_league(self, name):
        if name:
            league = League.objects.filter(name=name).first()
            if not league:
                raise CommandError(f'No league named "{name}".')
        else:
            one_pocket_leagues = list(League.objects.filter(results_type=League.ResultsType.ONE_POCKET))
            if not one_pocket_leagues:
                raise CommandError('No One Pocket league found. Pass --league to specify one.')
            if len(one_pocket_leagues) > 1:
                names = ', '.join(lg.name for lg in one_pocket_leagues)
                raise CommandError(f'Multiple One Pocket leagues found ({names}); pass --league to pick one.')
            league = one_pocket_leagues[0]

        if league.results_type != League.ResultsType.ONE_POCKET:
            raise CommandError(f'"{league.name}" is not a One Pocket league.')
        return league

    def _resolve_season(self, league, name):
        if name:
            season = Season.objects.filter(league=league, name=name).first()
            if not season:
                raise CommandError(f'No season named "{name}" for "{league.name}".')
            return season

        season = Season.objects.filter(league=league, status=Season.Status.ACTIVE).first()
        if not season:
            raise CommandError(f'No active season for "{league.name}". Pass --season to specify one.')
        return season

    def _render_pdf(self, matches, output_path):
        page_canvas = canvas.Canvas(output_path, pagesize=A4)
        width, height = A4
        margin = 1 * inch
        line_height = 0.55 * inch

        for match in matches:
            race_label = get_one_pocket_race_label(match.home_team, match.away_team)
            y = height - margin

            week = match.week
            week_label = f'Week {week.number}' if week.number is not None else str(week.date)
            page_canvas.setFont('Helvetica', 11)
            page_canvas.drawString(margin, y, f'{week_label} - {week.date}')
            y -= line_height

            page_canvas.setFont('Helvetica-Bold', 20)
            page_canvas.drawString(margin, y, f'Player 1: {match.home_team.name}')
            y -= line_height
            page_canvas.drawString(margin, y, f'Player 2: {match.away_team.name}')
            y -= line_height
            page_canvas.setFont('Helvetica', 18)
            page_canvas.drawString(margin, y, f'Race: {race_label or "___________"}')
            y -= line_height * 2

            for game_number in range(1, 6):
                page_canvas.drawString(
                    margin, y,
                    f'Game {game_number}: __________________    to: _______________________',
                )
                y -= line_height

            y -= line_height * 2
            page_canvas.setFont('Helvetica', 18)
            page_canvas.drawString(margin, y, 'Player 1 Signature: ______________________________________')
            y -= line_height * 2
            page_canvas.drawString(margin, y, 'Player 2 Signature: ______________________________________')

            page_canvas.showPage()

        page_canvas.save()
