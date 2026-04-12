import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import League, Team
from scheduling.models import Match, Season, Week


class Command(BaseCommand):
    help = 'Import season.json into the emcfunleague league.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            default='season.json',
            help='Path to the season JSON file.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate the import without creating anything in the database.',
        )

    def handle(self, *args, **options):
        file_path = Path(options['file'])
        dry_run = options['dry_run']

        self.stdout.write(self.style.NOTICE(f'Starting import from {file_path}...'))
        self.stdout.write(self.style.NOTICE(f'Dry run: {"yes" if dry_run else "no"}'))

        if not file_path.exists():
            raise CommandError(f'File not found: {file_path}')

        try:
            payload = json.loads(file_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise CommandError(f'Invalid JSON file: {exc}') from exc

        league_name = payload.get('league') or 'EMC Fun Pool League'
        if league_name != 'EMC Fun Pool League':
            raise CommandError(
                f'Unexpected league "{league_name}". This import only supports "EMC Fun Pool League".'
            )

        try:
            league = League.objects.get(name='EMC Fun Pool League')
        except League.DoesNotExist as exc:
            raise CommandError('League "EMC Fun Pool League" does not exist.') from exc

        self.stdout.write(self.style.NOTICE(f'Found league: {league.name}'))

        if Season.objects.filter(league=league, status=Season.Status.ACTIVE).exists():
            self.stdout.write(self.style.WARNING(
                'An active season already exists for emcfunleague. Nothing imported.'
            ))
            return

        season_name = payload.get('season_name')
        if not season_name:
            raise CommandError('JSON is missing "season_name".')

        weeks_data = payload.get('weeks')
        if not isinstance(weeks_data, list) or not weeks_data:
            raise CommandError('JSON is missing a non-empty "weeks" list.')

        week_numbers = [
            week_data.get('number')
            for week_data in weeks_data
            if week_data.get('number') is not None
        ]
        duplicate_numbers = sorted(
            number for number in set(week_numbers)
            if week_numbers.count(number) > 1
        )
        if duplicate_numbers:
            raise CommandError(
                f'Duplicate week number(s) found in JSON: {duplicate_numbers}'
            )

        team_cache = {
            team.name.strip().lower(): team
            for team in Team.objects.filter(league=league).select_related('venue')
        }

        self.stdout.write(self.style.NOTICE(f'Loaded {len(team_cache)} teams for league lookup'))

        def get_team(team_name: str) -> Team:
            team = team_cache.get(team_name.strip().lower())
            if team is None:
                raise CommandError(
                    f'Team "{team_name}" was not found on league "{league.name}".'
                )
            return team

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Dry run successful. Would import active season "{season_name}" for league "{league.name}".'
            ))
            self.stdout.write(self.style.NOTICE(f'Would create {len(weeks_data)} weeks'))
            return

        with transaction.atomic():
            self.stdout.write(self.style.NOTICE(f'Creating season "{season_name}"...'))
            season = Season.objects.create(
                league=league,
                name=season_name,
                status=Season.Status.ACTIVE,
            )

            created_weeks = {}

            for week_data in weeks_data:
                week_number = week_data.get('number')
                week_date = week_data.get('date')
                week_notes = week_data.get('notes', '')

                if not week_date:
                    raise CommandError(f'Week data is missing "date": {week_data!r}')

                self.stdout.write(self.style.NOTICE(
                    f'Creating week {week_number} for date {week_date}...'
                ))

                week, created = Week.objects.get_or_create(
                    season=season,
                    number=week_number,
                    defaults={
                        'date': week_date,
                        'notes': week_notes,
                    },
                )

                if not created:
                    self.stdout.write(self.style.WARNING(
                        f'Week {week_number} already existed; updating it.'
                    ))
                    week.date = week_date
                    week.notes = week_notes
                    week.save(update_fields=['date', 'notes'])

                created_weeks[week_number] = week

            for week_data in weeks_data:
                week_number = week_data.get('number')
                week = created_weeks.get(week_number)

                if week is None:
                    raise CommandError(f'Could not find created week {week_number}.')

                matches_data = week_data.get('matches', [])
                if not isinstance(matches_data, list):
                    raise CommandError(f'"matches" must be a list for week {week_number}.')

                self.stdout.write(self.style.NOTICE(
                    f'Importing {len(matches_data)} match(es) for week {week_number}...'
                ))

                for sort_order, match_data in enumerate(matches_data, start=1):
                    home_name = match_data.get('home_team')
                    away_name = match_data.get('away_team')

                    if not home_name or not away_name:
                        raise CommandError(
                            f'Match is missing home_team or away_team in week {week_number}: {match_data!r}'
                        )

                    if home_name == 'BYE' or away_name == 'BYE':
                        self.stdout.write(self.style.NOTICE(
                            f'Skipping bye entry in week {week_number}: {home_name} vs {away_name}'
                        ))
                        continue

                    home_team = get_team(home_name)
                    away_team = get_team(away_name)

                    self.stdout.write(self.style.NOTICE(
                        f'Creating match: week {week_number}, {home_team.name} vs {away_team.name}'
                    ))

                    Match.objects.create(
                        week=week,
                        home_team=home_team,
                        away_team=away_team,
                        location=match_data.get('venue', '') or home_team.venue.name,
                        sort_order=sort_order,
                    )

        self.stdout.write(self.style.SUCCESS(
            f'Successfully imported active season "{season_name}" for league "{league.name}".'
        ))