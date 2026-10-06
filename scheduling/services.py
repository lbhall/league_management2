from datetime import date, timedelta
import random

from django.db import transaction
from django.db.models import Sum, Q, IntegerField
from django.db.models.functions import Coalesce, Cast

from core.models import Team, Player, Venue
from scheduling.models import (
    ArchivedPlayer,
    ArchivedMatch,
    ArchivedPlayerMatchResult,
    ArchivedSeason,
    ArchivedTeam,
    Holiday,
    Match,
    Week,
)
from results.models import PlayerMatchResult, MatchResult


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


def _match_effective_venue(match):
    home_venue = match.home_team.venue
    location = (match.location or '').strip()
    if not location or location == home_venue.name:
        return home_venue
    venue = Venue.objects.filter(
        league_id=match.home_team.league_id,
        name=location,
    ).first()
    return venue or home_venue


def _week_match_count_at_effective_venue(week, venue_id, exclude_match_id=None):
    matches = week.matches.select_related('home_team__venue')
    if exclude_match_id is not None:
        matches = matches.exclude(pk=exclude_match_id)
    return sum(1 for m in matches if _match_effective_venue(m).id == venue_id)


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


def _season_has_results(season):
    return MatchResult.objects.filter(match__week__season=season).exists()


def compress_season_matches(season):
    """Pack every match into the earliest playable week that can take it
    (no team playing twice, venue capacity respected at the match's effective
    venue), then delete playable weeks left empty at the end of the season.

    Returns (moved_count, deleted_week_count). Refuses to touch a season that
    already has results recorded.
    """
    if _season_has_results(season):
        return 0, 0

    weeks = list(season.weeks.filter(number__isnull=False).order_by('date'))
    moved = 0

    for index, target_week in enumerate(weeks):
        for later_week in weeks[index + 1:]:
            matches = later_week.matches.select_related(
                'home_team__venue', 'away_team__venue',
            ).order_by('sort_order', 'id')
            for match in list(matches):
                if _week_has_team(target_week, match.home_team_id):
                    continue
                if _week_has_team(target_week, match.away_team_id):
                    continue
                venue = _match_effective_venue(match)
                if _week_match_count_at_effective_venue(target_week, venue.id) >= venue.max_home_teams:
                    continue
                match.week = target_week
                match.save(update_fields=['week'])
                moved += 1

    deleted = 0
    for week in reversed(weeks):
        if week.matches.exists():
            break
        week.delete()
        deleted += 1

    return moved, deleted


def _balanced_week_sizes(total, week_count):
    base, extra = divmod(total, week_count)
    return [base + 1] * extra + [base] * (week_count - extra)


def _try_pack(matches, venue_caps, sizes, rng):
    """One randomized first-fit attempt: place every match into a week slot
    respecting per-week size quotas, team uniqueness, and venue caps.
    Returns a list of per-week match lists, or None."""
    order = matches[:]
    rng.shuffle(order)
    weeks = [{'teams': set(), 'venues': {}, 'size': size, 'matches': []} for size in sizes]
    for match, venue in order:
        placed = False
        for week in weeks:
            if len(week['matches']) >= week['size']:
                continue
            if match.home_team_id in week['teams'] or match.away_team_id in week['teams']:
                continue
            if week['venues'].get(venue.id, 0) >= venue_caps[venue.id]:
                continue
            week['teams'] |= {match.home_team_id, match.away_team_id}
            week['venues'][venue.id] = week['venues'].get(venue.id, 0) + 1
            week['matches'].append(match)
            placed = True
            break
        if not placed:
            return None
    return weeks


def _polish_week_order(weeks, team_ids):
    """Reorder week contents (dates stay fixed) to spread byes and 3-match
    weeks: no team idle two weeks running where avoidable, full weeks at the
    season's start and end."""
    def penalty(ordering):
        pen = 0
        for team_id in team_ids:
            previous_bye = False
            for week in ordering:
                bye = team_id not in week['teams']
                if bye and previous_bye:
                    pen += 20
                previous_bye = bye
        sizes = [len(week['matches']) for week in ordering]
        largest = max(sizes)
        pen += 5 * (sizes[0] != largest)
        pen += 5 * (sizes[-1] != largest)
        for x, y in zip(sizes, sizes[1:]):
            if x < largest and y < largest:
                pen += 3
        return pen

    best, best_pen = weeks, penalty(weeks)
    improved = True
    while improved:
        improved = False
        for i in range(len(best)):
            for j in range(i + 1, len(best)):
                candidate = best[:]
                candidate[i], candidate[j] = candidate[j], candidate[i]
                p = penalty(candidate)
                if p < best_pen:
                    best, best_pen = candidate, p
                    improved = True
    return best


def pack_season_schedule(season, random_seed=None, attempts=5000):
    """Repack a season's matches into the fewest possible weeks.

    Computes the floor implied by venue capacities, team game counts, and
    weekly match limits, then searches for a balanced packing (randomized
    first-fit) and polishes the week order for bye spacing. Falls back to the
    greedy compress pass when no packing is found within the attempt budget.
    Matchups, home/away, and locations are never changed — only which week a
    match is played in. Refuses to touch a season with recorded results.

    Returns {'optimal': bool, 'moved': int, 'deleted_weeks': int, 'week_count': int}.
    """
    playable_weeks = list(season.weeks.filter(number__isnull=False).order_by('date'))
    matches = [
        (match, _match_effective_venue(match))
        for week in playable_weeks
        for match in week.matches.select_related('home_team__venue', 'away_team').order_by('sort_order', 'id')
    ]

    if _season_has_results(season):
        return {'optimal': False, 'moved': 0, 'deleted_weeks': 0, 'week_count': len(playable_weeks)}
    if not matches:
        return {'optimal': False, 'moved': 0, 'deleted_weeks': 0, 'week_count': len(playable_weeks)}

    venue_caps = {venue.id: venue.max_home_teams for _, venue in matches}
    hosted = {}
    games = {}
    for match, venue in matches:
        hosted[venue.id] = hosted.get(venue.id, 0) + 1
        for team_id in (match.home_team_id, match.away_team_id):
            games[team_id] = games.get(team_id, 0) + 1

    total = len(matches)
    max_per_week = min(len(games) // 2, sum(venue_caps.values()))
    floor = max(
        max(games.values()),
        -(-total // max_per_week),
        max(-(-hosted[vid] // venue_caps[vid]) for vid in hosted),
    )

    rng = random.Random(random_seed)
    packing = None
    week_count = floor
    while packing is None and week_count <= len(playable_weeks):
        sizes = _balanced_week_sizes(total, week_count)
        for _ in range(attempts):
            packing = _try_pack(matches, venue_caps, sizes, rng)
            if packing:
                break
        if packing is None:
            week_count += 1

    if packing is None:
        moved, deleted = compress_season_matches(season)
        return {
            'optimal': False, 'moved': moved, 'deleted_weeks': deleted,
            'week_count': season.weeks.filter(number__isnull=False).count(),
        }

    packing = _polish_week_order(packing, set(games))

    moved = 0
    with transaction.atomic():
        for week, slot in zip(playable_weeks, packing):
            for sort_order, match in enumerate(slot['matches'], start=1):
                if match.week_id != week.id or match.sort_order != sort_order:
                    moved += match.week_id != week.id
                    match.week = week
                    match.sort_order = sort_order
                    match.save(update_fields=['week', 'sort_order'])
        deleted = 0
        for week in playable_weeks[week_count:]:
            week.delete()
            deleted += 1

    return {'optimal': True, 'moved': moved, 'deleted_weeks': deleted, 'week_count': week_count}


def create_mirrored_season_schedule(season):
    existing_weeks = list(season.weeks.order_by('date', 'number'))
    if not existing_weeks:
        return []

    # Snapshot before creating anything: mirrors may be placed into later
    # existing weeks, and must not be picked up and mirrored again.
    matches_to_mirror = [
        match
        for original_week in existing_weeks
        for match in original_week.matches.order_by('sort_order', 'id')
    ]

    created_weeks = []

    for match in matches_to_mirror:
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

    # Final pass: the greedy placement above (and the original half's layout)
    # can leave gaps — repack to the fewest possible weeks (falls back to the
    # greedy compressor internally if no optimal packing is found).
    pack_season_schedule(season)

    surviving_ids = set(season.weeks.values_list('id', flat=True))
    return [week for week in created_weeks if week.id in surviving_ids]


def get_valid_destination_weeks(season, match):
    venue = _match_effective_venue(match)
    valid_weeks = []
    for week in season.weeks.order_by('date', 'number'):
        if week.number is None:
            continue
        if _week_match_count_at_effective_venue(week, venue.id) < venue.max_home_teams:
            valid_weeks.append(week)
    return valid_weeks


def renumber_weeks(season):
    with transaction.atomic():
        weeks = list(season.weeks.order_by('date'))
        if not weeks:
            return 0

        Week.objects.filter(pk__in=[w.id for w in weeks]).update(number=None)

        next_number = 1
        for w in weeks:
            is_holiday = w.number is None or bool((w.notes or '').strip())
            if is_holiday:
                continue
            Week.objects.filter(pk=w.id).update(number=next_number)
            next_number += 1

        return next_number - 1


def delete_week(week):
    if week.number is None:
        raise ValueError('Holiday weeks cannot be deleted from the schedule view.')
    if week.matches.exists():
        raise ValueError('Cannot delete a week that still has matches scheduled.')
    week.delete()


def move_match_to_week(match, target_week):
    if match.week.season_id != target_week.season_id:
        raise ValueError('Target week must belong to the same season.')

    if target_week.number is None:
        raise ValueError('Matches cannot be moved onto a holiday week.')

    venue = _match_effective_venue(match)
    current_count = _week_match_count_at_effective_venue(
        target_week, venue.id, exclude_match_id=match.pk,
    )

    if current_count >= venue.max_home_teams:
        raise ValueError('Target week venue capacity would be exceeded.')

    match.week = target_week
    match.full_clean()
    match.save(update_fields=['week'])


def get_venue_violations(season):
    """
    Return a list of dicts describing weeks where a venue has more matches
    than its max_home_teams capacity allows.

    Each dict has: week, venue, match_count, allowed.
    """
    violations = []
    weeks = list(
        season.weeks.prefetch_related(
            'matches__home_team__venue',
        ).order_by('date', 'number')
    )

    for week in weeks:
        if week.number is None:
            continue

        venue_counts = {}
        for match in week.matches.all():
            venue = _match_effective_venue(match)
            venue_counts.setdefault(venue.id, {'venue': venue, 'count': 0})
            venue_counts[venue.id]['count'] += 1

        for entry in venue_counts.values():
            if entry['count'] > entry['venue'].max_home_teams:
                violations.append({
                    'week': week,
                    'venue': entry['venue'],
                    'match_count': entry['count'],
                    'allowed': entry['venue'].max_home_teams,
                })

    return violations


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
            venue = _match_effective_venue(match)
            venue_counts.setdefault(venue.id, {'venue': venue, 'matches': []})
            venue_counts[venue.id]['matches'].append(match)

        for entry in venue_counts.values():
            venue = entry['venue']
            venue_matches = entry['matches']
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

    # Archive Matches and PlayerMatchResults if it's one_pocket
    if season.league.results_type == 'one_pocket':
        for week in weeks:
            for match in week.matches.all():
                try:
                    res = match.result
                except MatchResult.DoesNotExist:
                    res = None

                archived_match = ArchivedMatch.objects.create(
                    archived_season=archived_season,
                    date=week.date,
                    home_team_name=match.home_team.name,
                    away_team_name=match.away_team.name,
                    home_team_score=res.home_team_score if res else None,
                    away_team_score=res.away_team_score if res else None,
                )

                if res:
                    # For one_pocket, we use the team's player (since team_size is 1)
                    hp = Player.objects.filter(team=match.home_team).first()
                    ap = Player.objects.filter(team=match.away_team).first()

                    if hp:
                        ArchivedPlayerMatchResult.objects.create(
                            archived_match=archived_match,
                            player_name=hp.name,
                            team_name=match.home_team.name,
                            wins=res.home_team_score or 0,
                            losses=res.away_team_score or 0,
                        )
                    if ap:
                        ArchivedPlayerMatchResult.objects.create(
                            archived_match=archived_match,
                            player_name=ap.name,
                            team_name=match.away_team.name,
                            wins=res.away_team_score or 0,
                            losses=res.home_team_score or 0,
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
    if season.league.results_type == 'one_pocket':
        # For one_pocket, calculate stats from MatchResults because PlayerMatchResults might not exist
        player_stats = {}
        matches = Match.objects.filter(week__season=season).select_related('result', 'home_team', 'away_team')
        for match in matches:
            if not hasattr(match, 'result'):
                continue
            
            res = match.result
            # Home player
            hp = Player.objects.filter(team=match.home_team).first()
            if hp:
                if hp.id not in player_stats:
                    player_stats[hp.id] = {'games_won': 0, 'games_lost': 0, 'run_outs': 0, 'eight_on_the_breaks': 0, 'sweeps': 0}
                player_stats[hp.id]['games_won'] += res.home_team_score or 0
                player_stats[hp.id]['games_lost'] += res.away_team_score or 0
            
            # Away player
            ap = Player.objects.filter(team=match.away_team).first()
            if ap:
                if ap.id not in player_stats:
                    player_stats[ap.id] = {'games_won': 0, 'games_lost': 0, 'run_outs': 0, 'eight_on_the_breaks': 0, 'sweeps': 0}
                player_stats[ap.id]['games_won'] += res.away_team_score or 0
                player_stats[ap.id]['games_lost'] += res.home_team_score or 0
        
        return player_stats

    player_results = (
        PlayerMatchResult.objects.filter(match_result__match__week__season=season)
        .values('player_id')
        .annotate(
            games_won=Coalesce(Sum('wins'), 0),
            games_lost=Coalesce(Sum('losses'), 0),
            run_outs=Coalesce(Sum('runouts'), 0),
            eight_on_the_breaks=Coalesce(Sum('eight_on_the_breaks'), 0),
            sweeps=Coalesce(Sum(Cast('won_all_games', IntegerField())), 0),
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