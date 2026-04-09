from datetime import date, timedelta
import random

from django.db import transaction
from django.db.models import Sum, F, Value, Q
from django.db.models.functions import Coalesce

from core.models import Team, Player
from scheduling.models import (
    ArchivedPlayer,
    ArchivedSeason,
    ArchivedTeam,
    Holiday,
    Match,
    Week,
)
from results.models import PlayerMatchResult


DAY_NAME_TO_WEEKDAY = {
    'monday': 0,
    'tuesday': 1,
    'wednesday': 2,
    'thursday': 3,
    'friday': 4,
    'saturday': 5,
    'sunday': 6,
}

def get_next_start_dates(league, from_date, count=5):
    weekday = DAY_NAME_TO_WEEKDAY[league.day_of_week]
    days_until = (weekday - from_date.weekday()) % 7
    if days_until == 0:
        days_until = 7

    first_date = from_date + timedelta(days=days_until)
    return [first_date + timedelta(weeks=index) for index in range(count)]


def assign_random_team_seeds(league, random_seed=None):
    teams = list(
        Team.objects.filter(league=league).order_by('name')
    )

    Team.objects.filter(league=league).update(seed=None)

    rng = random.Random(random_seed)
    rng.shuffle(teams)

    for index, team in enumerate(teams, start=1):
        team.seed = index

    Team.objects.bulk_update(teams, ['seed'])

    return teams


def get_seeded_teams(league):
    teams = list(
        Team.objects.filter(league=league)
        .select_related('venue')
        .order_by('seed', 'name')
    )

    seeded = [team for team in teams if team.seed is not None]
    unseeded = [team for team in teams if team.seed is None]

    return seeded + unseeded


def generate_round_robin_pairings(teams, random_seed=None):
    teams = list(teams)

    if len(teams) < 2:
        return []

    rng = random.Random(random_seed)

    working_teams = teams[:]
    if len(working_teams) % 2 == 1:
        working_teams.append(None)

    if len(working_teams) > 2:
        first_team = working_teams[0]
        rotating = working_teams[1:]
        rng.shuffle(rotating)
        working_teams = [first_team] + rotating

    rounds = []
    team_count = len(working_teams)

    for round_index in range(team_count - 1):
        round_matches = []

        for match_index in range(team_count // 2):
            home_team = working_teams[match_index]
            away_team = working_teams[team_count - 1 - match_index]

            if home_team is None or away_team is None:
                continue

            if round_index % 2 == 0:
                round_matches.append((home_team, away_team))
            else:
                round_matches.append((away_team, home_team))

        rounds.append(round_matches)

        fixed_team = working_teams[0]
        rotating = working_teams[1:]
        rotating = [rotating[-1]] + rotating[:-1]
        working_teams = [fixed_team] + rotating

    return rounds


def _get_holiday_for_date(week_date):
    return Holiday.objects.filter(date=week_date).order_by('description').first()


def _create_week_for_date(season, week_date, number):
    holiday = _get_holiday_for_date(week_date)

    if holiday:
        return Week.objects.create(
            season=season,
            date=week_date,
            number=None,
            notes=holiday.description,
        )

    return Week.objects.create(
        season=season,
        date=week_date,
        number=number,
    )


def week_can_accept_match(week, home_team, away_team):
    if week.number is None:
        return False

    if week.matches.filter(
        Q(home_team=home_team) | Q(away_team=home_team) |
        Q(home_team=away_team) | Q(away_team=away_team)
    ).exists():
        return False

    home_count = week.matches.filter(home_team__venue=home_team.venue).count()
    if home_count >= home_team.venue.max_home_teams:
        return False

    return True


def _week_has_team(week, team_id):
    return week.matches.filter(
        Q(home_team_id=team_id) | Q(away_team_id=team_id)
    ).exists()


def _week_home_match_count(week, venue_id):
    return week.matches.filter(home_team__venue_id=venue_id).count()


def _find_week_for_match(candidate_weeks, home_team, away_team):
    venue_id = home_team.venue_id
    max_home_teams = home_team.venue.max_home_teams

    for week in candidate_weeks:
        if week.number is None:
            continue

        if _week_home_match_count(week, venue_id) >= max_home_teams:
            continue

        if _week_has_team(week, home_team.id):
            continue

        if _week_has_team(week, away_team.id):
            continue

        return week

    return None


def _next_playable_week_number(season):
    return season.weeks.filter(number__isnull=False).count() + 1


def _create_next_week(season, last_week, number):
    next_date = last_week.date + timedelta(weeks=1)
    return _create_week_for_date(
        season=season,
        week_date=next_date,
        number=number,
    )


def create_new_playable_week_at_end(season):
    latest_week = season.weeks.order_by('date', 'number').last()
    next_number = _next_playable_week_number(season)

    if latest_week is None:
        raise ValueError('Season must have at least one week before adding a new week at the end.')

    new_week = _create_next_week(
        season=season,
        last_week=latest_week,
        number=next_number,
    )

    while new_week.number is None:
        latest_week = new_week
        new_week = _create_next_week(
            season=season,
            last_week=latest_week,
            number=next_number,
        )

    return new_week


def recreate_season_schedule(season, start_date, random_seed=None):
    season.weeks.all().delete()

    assign_random_team_seeds(season.league, random_seed=random_seed)
    teams = get_seeded_teams(season.league)
    rounds = generate_round_robin_pairings(teams, random_seed=random_seed)

    if not rounds:
        return []

    created_weeks = []
    current_date = start_date
    week_number = 1

    while len([week for week in created_weeks if week.number is not None]) < len(rounds):
        week = _create_week_for_date(
            season=season,
            week_date=current_date,
            number=week_number,
        )
        created_weeks.append(week)

        if week.number is not None:
            week_number += 1

        current_date += timedelta(weeks=1)

    for round_matches in rounds:
        for match_index, (home_team, away_team) in enumerate(round_matches, start=1):
            target_week = _find_week_for_match(created_weeks, home_team, away_team)

            while target_week is None:
                last_week = created_weeks[-1]
                target_week = _create_next_week(
                    season=season,
                    last_week=last_week,
                    number=week_number,
                )
                created_weeks.append(target_week)

                if target_week.number is not None:
                    week_number += 1
                else:
                    target_week = None

            Match.objects.create(
                week=target_week,
                home_team=home_team,
                away_team=away_team,
                location=home_team.venue.name,
                sort_order=match_index,
            )

            created_weeks = sorted(created_weeks, key=lambda week: week.date)

    return created_weeks


def create_mirrored_season_schedule(season):
    existing_weeks = list(season.weeks.order_by('date', 'number'))
    if not existing_weeks:
        return []

    created_weeks = []

    for original_week in existing_weeks:
        for match in original_week.matches.order_by('sort_order', 'id'):
            target_week = _find_week_for_match(existing_weeks+created_weeks, match.away_team, match.home_team)

            if target_week is None:
                target_week = create_new_playable_week_at_end(season)
                created_weeks.append(target_week)

            Match.objects.create(
                week=target_week,
                home_team=match.away_team,
                away_team=match.home_team,
                location=match.away_team.venue.name,
                sort_order=match.sort_order,
            )

    return created_weeks


def get_valid_destination_weeks(season, match):
    valid_weeks = []
    for week in season.weeks.order_by('date', 'number'):
        if week.number is None:
            continue
        if _week_home_match_count(week, match.home_team.venue_id) < match.home_team.venue.max_home_teams:
            valid_weeks.append(week)
    return valid_weeks


def move_match_to_week(match, target_week):
    if match.week.season_id != target_week.season_id:
        raise ValueError('Target week must belong to the same season.')

    if target_week.number is None:
        raise ValueError('Matches cannot be moved onto a holiday week.')

    current_count = target_week.matches.filter(
        home_team__venue_id=match.home_team.venue_id
    ).exclude(pk=match.pk).count()

    if current_count >= match.home_team.venue.max_home_teams:
        raise ValueError('Target week venue capacity would be exceeded.')

    match.week = target_week
    match.full_clean()
    match.save(update_fields=['week'])


def rebalance_season_matches(season):
    weeks = list(season.weeks.order_by('date', 'number'))
    if not weeks:
        return []

    moved_matches = []

    for week in weeks:
        if week.number is None:
            continue

        venue_counts = {}

        for match in week.matches.select_related('home_team__venue').order_by('sort_order', 'id'):
            venue_id = match.home_team.venue_id
            venue_counts.setdefault(venue_id, [])
            venue_counts[venue_id].append(match)

        for venue_matches in venue_counts.values():
            if not venue_matches:
                continue

            venue = venue_matches[0].home_team.venue
            allowed = venue.max_home_teams

            if len(venue_matches) <= allowed:
                continue

            overflow_matches = venue_matches[allowed:]

            for overflow_match in overflow_matches:
                future_weeks = [
                    candidate_week
                    for candidate_week in season.weeks.order_by('date', 'number')
                    if candidate_week.date > week.date and candidate_week.number is not None
                ]

                target_week = _find_week_for_match(future_weeks, overflow_match.home_team, overflow_match.away_team)

                while target_week is None:
                    latest_week = season.weeks.order_by('date', 'number').last()
                    target_week = _create_next_week(
                        season=season,
                        last_week=latest_week,
                        number=(latest_week.number + 1) if latest_week.number is not None else season.weeks.filter(number__isnull=False).count() + 1,
                    )
                    if target_week.number is None:
                        target_week = None

                overflow_match.week = target_week
                overflow_match.save(update_fields=['week'])
                moved_matches.append(overflow_match)

    return moved_matches

@transaction.atomic
def archive_season(season):
    weeks = list(season.weeks.all().order_by('date', 'number'))
    if not weeks:
        raise ValueError('Season has no weeks to archive.')

    beginning_date = weeks[0].date
    ending_date = weeks[-1].date
    archived_season_name = f'{beginning_date} - {ending_date}'

    archived_season = ArchivedSeason.objects.create(
        league=season.league,
        name=archived_season_name,
    )

    team_stats = build_team_archive_stats(season)
    for team in Team.objects.filter(league=season.league).order_by('name'):
        stats = team_stats.get(team.id, {})
        ArchivedTeam.objects.create(
            archived_season=archived_season,
            team_name=team.name,
            matches_won=stats.get('matches_won', 0),
            matches_lost=stats.get('matches_lost', 0),
            games_won=stats.get('games_won', 0),
            games_lost=stats.get('games_lost', 0),
        )

    player_stats = build_player_archive_stats(season)
    for player in Player.objects.filter(league=season.league).select_related('team').order_by('name'):
        stats = player_stats.get(player.id, {})
        ArchivedPlayer.objects.create(
            archived_season=archived_season,
            player_name=player.name,
            team_name=player.team.name if player.team else '',
            games_won=stats.get('games_won', 0),
            games_lost=stats.get('games_lost', 0),
            run_outs=stats.get('run_outs', 0),
            eight_on_the_breaks=stats.get('eight_on_the_breaks', 0),
            sweeps=stats.get('sweeps', 0),
        )

    season.delete()
    return archived_season


def build_team_archive_stats(season):
    # Reuse your current standings logic here if you prefer.
    # This keeps the archive data aligned with the live stats.
    from core.views import build_team_standings

    standings = build_team_standings(season.league, season)
    return {
        row['team_id']: {
            'matches_won': row['matches_won'],
            'matches_lost': row['matches_lost'],
            'games_won': row['games_won'],
            'games_lost': row['games_lost'],
        }
        for row in standings
    }


def build_player_archive_stats(season):
    player_results = (
        PlayerMatchResult.objects.filter(match_result__match__week__season=season)
        .values('player_id')
        .annotate(
            games_won=Coalesce(Sum('wins'), 0),
            games_lost=Coalesce(Sum('losses'), 0),
            run_outs=Coalesce(Sum('runouts'), 0),
            eight_on_the_breaks=Coalesce(Sum('eight_on_the_breaks'), 0),
            sweeps=Coalesce(Sum('won_all_games'), 0),
        )
    )

    return {
        row['player_id']: {
            'games_won': row['games_won'],
            'games_lost': row['games_lost'],
            'run_outs': row['run_outs'],
            'eight_on_the_breaks': row['eight_on_the_breaks'],
            'sweeps': row['sweeps'],
        }
        for row in player_results
    }

def _get_swap_placeholder_date(season):
    existing_dates = set(season.weeks.values_list('date', flat=True))

    candidate_dates = [
        date(1, 1, 1),
        date(9999, 12, 31),
        date(1900, 1, 1),
        date(2100, 1, 1),
    ]

    for candidate in candidate_dates:
        if candidate not in existing_dates:
            return candidate

    raise ValueError('Unable to find a temporary date for moving weeks.')


def _renumber_season_weeks(season):
    weeks = list(season.weeks.order_by('date', 'id'))
    playable_weeks = [week for week in weeks if week.number is not None]

    if not playable_weeks:
        return

    # Clear numbers first so we don't collide on the unique constraint.
    for week in playable_weeks:
        week.number = None
        week.save(update_fields=['number'])

    # Reassign in date order.
    for index, week in enumerate(playable_weeks, start=1):
        week.number = index
        week.save(update_fields=['number'])


@transaction.atomic
def move_week_up(week):
    season_weeks = list(week.season.weeks.order_by('date', 'number', 'id'))
    current_index = season_weeks.index(week)

    if current_index == 0:
        raise ValueError('The first week of the season cannot be moved up.')

    previous_week = season_weeks[current_index - 1]
    placeholder_date = _get_swap_placeholder_date(week.season)

    previous_week_date = previous_week.date
    week_date = week.date

    previous_week.date = placeholder_date
    previous_week.save(update_fields=['date'])

    week.date = previous_week_date
    week.save(update_fields=['date'])

    previous_week.date = week_date
    previous_week.save(update_fields=['date'])

    _renumber_season_weeks(week.season)
    return week


@transaction.atomic
def move_week_down(week):
    season_weeks = list(week.season.weeks.order_by('date', 'number', 'id'))
    current_index = season_weeks.index(week)

    if current_index == len(season_weeks) - 1:
        raise ValueError('The last week of the season cannot be moved down.')

    next_week = season_weeks[current_index + 1]
    placeholder_date = _get_swap_placeholder_date(week.season)

    next_week_date = next_week.date
    week_date = week.date

    next_week.date = placeholder_date
    next_week.save(update_fields=['date'])

    week.date = next_week_date
    week.save(update_fields=['date'])

    next_week.date = week_date
    next_week.save(update_fields=['date'])

    _renumber_season_weeks(week.season)
    return week