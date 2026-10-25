from datetime import date
import logging

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone

from decimal import Decimal

from content.models import NewsItem, Rule
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import (
    ArchivedPlayerMatchResult,
    ArchivedSeason,
    Match,
    Season,
    Week,
)
from .models import League, Player, Team


ONE_POCKET_RACE_ROWS = [
    {'higher_rank': 5, 'lower_rank': 5, 'higher_race': 8, 'lower_race': 8},
    {'higher_rank': 5, 'lower_rank': 4, 'higher_race': 9, 'lower_race': 7},
    {'higher_rank': 5, 'lower_rank': 3, 'higher_race': 10, 'lower_race': 6},
    {'higher_rank': 5, 'lower_rank': 2, 'higher_race': 11, 'lower_race': 5},
    {'higher_rank': 5, 'lower_rank': 1, 'higher_race': 12, 'lower_race': 5},
    {'higher_rank': 4, 'lower_rank': 4, 'higher_race': 8, 'lower_race': 8},
    {'higher_rank': 4, 'lower_rank': 3, 'higher_race': 9, 'lower_race': 7},
    {'higher_rank': 4, 'lower_rank': 2, 'higher_race': 10, 'lower_race': 6},
    {'higher_rank': 4, 'lower_rank': 1, 'higher_race': 11, 'lower_race': 5},
    {'higher_rank': 3, 'lower_rank': 3, 'higher_race': 8, 'lower_race': 8},
    {'higher_rank': 3, 'lower_rank': 2, 'higher_race': 9, 'lower_race': 7},
    {'higher_rank': 3, 'lower_rank': 1, 'higher_race': 10, 'lower_race': 6},
    {'higher_rank': 2, 'lower_rank': 2, 'higher_race': 8, 'lower_race': 8},
    {'higher_rank': 2, 'lower_rank': 1, 'higher_race': 9, 'lower_race': 7},
    {'higher_rank': 1, 'lower_rank': 1, 'higher_race': 8, 'lower_race': 8},
]


logging.basicConfig(
    filename='league.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def get_active_league(request):
    requested_league_id = request.GET.get('league')
    if requested_league_id:
        request.session['frontend_league_id'] = requested_league_id

    # Explicit ?league= param or session takes highest priority.
    league_id = request.session.get('frontend_league_id')

    # Next: match the incoming Host header against the LEAGUE_HOSTS map so a
    # single container can serve multiple leagues differentiated by domain.
    if not league_id:
        host = request.get_host().split(':')[0]  # strip port if present
        league_id = settings.LEAGUE_HOSTS.get(host)

    # Finally fall back to the single-league FRONTEND_LEAGUE_ID setting.
    if not league_id:
        league_id = settings.FRONTEND_LEAGUE_ID

    if league_id:
        league = League.objects.filter(pk=league_id).first()
        if league:
            return league

    return League.objects.order_by('name').first()


def get_active_season(active_league):
    if not active_league:
        return None

    return Season.objects.filter(
        league=active_league,
        status=Season.Status.ACTIVE,
    ).first()


def get_result_weeks(active_season):
    if not active_season:
        return Week.objects.none()

    return (
        Week.objects.filter(
            season=active_season,
            number__isnull=False,
            matches__result__isnull=False,
        )
        .distinct()
        .order_by('date', 'number')
    )


# A one-pocket match counts as finished once either side reaches this score.
ONE_POCKET_COMPLETE_SCORE = 3


def get_one_pocket_races(rank_a, rank_b):
    """Return (race_a, race_b) for two skill ranks, or None if unknown."""
    if rank_a is None or rank_b is None:
        return None

    higher_rank = max(rank_a, rank_b)
    lower_rank = min(rank_a, rank_b)

    race_lookup = {
        (row['higher_rank'], row['lower_rank']): (row['higher_race'], row['lower_race'])
        for row in ONE_POCKET_RACE_ROWS
    }

    race = race_lookup.get((higher_rank, lower_rank))
    if not race:
        return None

    higher_race, lower_race = race
    race_a = higher_race if rank_a == higher_rank else lower_race
    race_b = higher_race if rank_b == higher_rank else lower_race
    return race_a, race_b


def get_one_pocket_race_label(team_a, team_b):
    races = get_one_pocket_races(team_a.team_rank, team_b.team_rank)
    if not races:
        return ''
    race_a, race_b = races
    return f'{race_a}/{race_b}'


def build_week_schedule_with_byes(active_league, week, team_standings=None):
    teams = list(
        Team.objects.filter(league=active_league).order_by('name')
    )
    matches = list(
        week.matches.all()
    )

    rank_map = {}
    if team_standings:
        rank_map = {s['team_id']: s['league_rank'] for s in team_standings}

    scheduled_team_ids = set()
    for match in matches:
        scheduled_team_ids.add(match.home_team_id)
        scheduled_team_ids.add(match.away_team_id)

    bye_entries = []
    for team in teams:
        if team.id not in scheduled_team_ids:
            bye_entries.append({
                'home_team': team,
                'home_team_rank': rank_map.get(team.id),
                'home_team_skill_rank': team.team_rank,
                'away_team': 'BYE',
                'location': '',
                'is_bye': True,
                'race_label': '',
            })

    match_entries = [
        {
            'home_team': match.home_team,
            'home_team_rank': rank_map.get(match.home_team_id),
            'home_team_skill_rank': match.home_team.team_rank,
            'away_team': match.away_team,
            'away_team_rank': rank_map.get(match.away_team_id),
            'away_team_skill_rank': match.away_team.team_rank,
            'location': match.location,
            'is_bye': False,
            'race_label': (
                get_one_pocket_race_label(match.home_team, match.away_team)
                if active_league.results_type == League.ResultsType.ONE_POCKET
                else ''
            ),
            'result_label': (
                f'{match.result.home_team_score or 0}-{match.result.away_team_score or 0}'
                if hasattr(match, 'result')
                and match.result.home_team_score is not None
                and match.result.away_team_score is not None
                and (
                    active_league.results_type != League.ResultsType.ONE_POCKET
                    or max(match.result.home_team_score, match.result.away_team_score) >= 3
                )
                else ''
            ),
        }
        for match in matches
    ]

    return match_entries + bye_entries


def build_team_standings(active_league, active_season, through_week=None):
    teams = list(
        Team.objects.filter(league=active_league)
        .exclude(name__iexact='BYE')
        .order_by('name')
    )

    standings_map = {
        team.id: {
            'team_id': team.id,
            'team': team.name,
            'team_rank': team.team_rank,
            'matches_won': 0,
            'matches_lost': 0,
            'games_won': 0,
            'games_lost': 0,
        }
        for team in teams
    }

    if not active_season:
        return sorted(
            standings_map.values(),
            key=lambda standing: (
                -standing['matches_won'],
                standing['matches_lost'],
                -standing['games_won'],
                standing['games_lost'],
                standing['team'],
            ),
        )

    matches = Match.objects.filter(
        week__season=active_season,
    )

    if through_week is not None:
        matches = matches.filter(
            week__date__lte=through_week.date,
        )

    matches = (
        matches
        .select_related('home_team', 'away_team', 'week')
        .prefetch_related(
            Prefetch(
                'result__player_results',
                queryset=PlayerMatchResult.objects.select_related('player', 'represented_team'),
            )
        )
        .order_by('week__date', 'sort_order', 'id')
    )

    total_games_per_match = active_league.team_size * active_league.team_size
    match_win_threshold = total_games_per_match / 2

    for match in matches:
        if not hasattr(match, 'result'):
            continue

        if active_league.results_type in (League.ResultsType.ONE_POCKET, League.ResultsType.DARTS):
            home_games_won = match.result.home_team_score or 0
            away_games_won = match.result.away_team_score or 0
            home_games_lost = away_games_won
            away_games_lost = home_games_won
        else:
            home_games_won = 0
            away_games_won = 0

            for player_result in match.result.player_results.all():
                if player_result.represented_team_id == match.home_team_id:
                    home_games_won += player_result.wins
                elif player_result.represented_team_id == match.away_team_id:
                    away_games_won += player_result.wins

            home_games_lost = total_games_per_match - home_games_won
            away_games_lost = total_games_per_match - away_games_won

        home_standing = standings_map.get(match.home_team_id)
        away_standing = standings_map.get(match.away_team_id)

        if home_standing:
            home_standing['games_won'] += home_games_won
            home_standing['games_lost'] += home_games_lost

        if away_standing:
            away_standing['games_won'] += away_games_won
            away_standing['games_lost'] += away_games_lost

        if active_league.results_type in (League.ResultsType.ONE_POCKET, League.ResultsType.DARTS):
            if home_games_won > away_games_won:
                if home_standing:
                    home_standing['matches_won'] += 1
                if away_standing:
                    away_standing['matches_lost'] += 1
            elif away_games_won > home_games_won:
                if away_standing:
                    away_standing['matches_won'] += 1
                if home_standing:
                    home_standing['matches_lost'] += 1
        else:
            if home_games_won > match_win_threshold:
                if home_standing:
                    home_standing['matches_won'] += 1
                if away_standing:
                    away_standing['matches_lost'] += 1
            elif away_games_won > match_win_threshold:
                if away_standing:
                    away_standing['matches_won'] += 1
                if home_standing:
                    home_standing['matches_lost'] += 1

    sorted_standings = sorted(
        standings_map.values(),
        key=lambda standing: (
            -standing['matches_won'],
            standing['matches_lost'],
            -standing['games_won'],
            standing['games_lost'],
            standing['team'],
        ),
    )

    for i, standing in enumerate(sorted_standings, start=1):
        standing['league_rank'] = i

    return sorted_standings


def build_player_stats(active_league, active_season, through_week=None):
    players = list(
        Player.objects.filter(league=active_league)
        .select_related('team')
        .order_by('name')
    )

    stats_map = {
        player.id: {
            'team_id': player.team.id if player.team else '',
            'player_id': player.id,
            'player': player.name,
            'team': player.team.name if player.team else '',
            'male': player.male,
            'wins': 0,
            'losses': 0,
            'percentage': 0.0,
            'runs': 0,
            'sweeps': 0,
            'eights': 0,
            'hat_tricks': 0,
            'three_in_a_beds': 0,
            'white_horses': 0,
            'three_in_the_blacks': 0,
            'points': 0,
            'games_played': 0,
            'tie_breaker': 0,
        }
        for player in players
    }

    is_darts = active_league.results_type == League.ResultsType.DARTS

    def darts_sort_key(stat):
        return (
            -stat['points'],
            -stat['hat_tricks'],
            -stat['three_in_a_beds'],
            -stat['white_horses'],
            -stat['three_in_the_blacks'],
            stat['player'],
        )

    def pool_sort_key(stat):
        return (
            -stat['wins'],
            -stat['tie_breaker'],
            -stat['percentage'],
            stat['player'],
        )

    sort_key = darts_sort_key if is_darts else pool_sort_key

    if not active_season:
        return sorted(stats_map.values(), key=sort_key)

    player_results = PlayerMatchResult.objects.filter(
        match_result__match__week__season=active_season,
    )

    if through_week is not None:
        player_results = player_results.filter(
            match_result__match__week__date__lte=through_week.date,
        )

    player_results = player_results.select_related('player').order_by('player__name')

    for player_result in player_results:
        if player_result.player_id not in stats_map:
            continue

        stats_map[player_result.player_id]['wins'] += player_result.wins
        stats_map[player_result.player_id]['losses'] += player_result.losses
        stats_map[player_result.player_id]['runs'] += player_result.runouts
        stats_map[player_result.player_id]['sweeps'] += 1 if player_result.won_all_games else 0
        stats_map[player_result.player_id]['eights'] += player_result.eight_on_the_breaks
        stats_map[player_result.player_id]['hat_tricks'] += player_result.hat_tricks
        stats_map[player_result.player_id]['three_in_a_beds'] += player_result.three_in_a_beds
        stats_map[player_result.player_id]['white_horses'] += player_result.white_horses
        stats_map[player_result.player_id]['three_in_the_blacks'] += player_result.three_in_the_blacks
        stats_map[player_result.player_id]['points'] += player_result.darts_points
        stats_map[player_result.player_id]['games_played'] += 1

    for stat in stats_map.values():
        stat['tie_breaker'] = stat['sweeps'] + stat['eights'] + stat['runs'] + stat['points']
        total = stat['wins'] + stat['losses']
        stat['percentage'] = ((stat['wins'] / total) * 100) if total > 0 else 0.0

    return sorted(stats_map.values(), key=sort_key)


@staff_member_required
def finance(request):
    logging.info(f'Finance Page -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers.get("Host")}, user-agent: {request.headers.get("User-Agent")}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    if not active_league:
        raise Http404("No active league found.")

    teams = active_league.teams.all().order_by('name')
    team_count = teams.count()
    team_size = active_league.team_size

    weeks_played = active_season.weeks.filter(
        number__isnull=False,
        matches__result__isnull=False
    ).distinct().count() if active_season else 0

    signup_fee = active_league.signup_fee
    fee_per_player = active_league.fee_per_player
    greens_fee = active_league.greens_fee
    operator_pay_per_player = active_league.operator_pay_per_player
    tournament_target = active_league.tournament_target

    weekly_payout_pool_per_team = (fee_per_player - greens_fee - operator_pay_per_player) * Decimal(team_size)
    total_matches_in_season = Match.objects.filter(week__season=active_season, week__number__isnull=False).count()
    total_weekly_payout_pool = weekly_payout_pool_per_team * Decimal(total_matches_in_season) * Decimal('2')
    tournament_money = tournament_target

    # Current money calculation
    total_matches_played = Match.objects.filter(
        week__season=active_season,
        week__number__isnull=False,
        result__isnull=False
    ).count()
    current_weekly_net = weekly_payout_pool_per_team * Decimal(total_matches_played) * Decimal('2')
    current_balance = current_weekly_net

    standings_data = []
    player_stats_data = []
    if active_season:
        standings_data = build_team_standings(active_league, active_season)
        player_stats_data = build_player_stats(active_league, active_season)

    def get_top_players(players, predicate=None, stat_key='wins', tie_breakers=[]):
        filtered = [
            p for p in players
            if (predicate(p) if predicate else True)
               and (p['wins'] + p['losses']) > 0
        ]
        if not filtered:
            return []

        # Sort by all keys descending
        sort_keys = [stat_key] + tie_breakers
        filtered.sort(key=lambda p: tuple(-p.get(k, 0) for k in sort_keys))
        
        top_players = []
        if filtered:
            best = filtered[0]
            if best.get(stat_key, 0) > 0:
                for p in filtered:
                    if all(p.get(k) == best.get(k) for k in sort_keys):
                        top_players.append(p)
                    else:
                        break
        return top_players

    def get_top_by_record(players, is_male):
        candidates = [
            p for p in players
            if p['male'] == is_male and (p['wins'] + p['losses']) > 0
        ]
        if not candidates or candidates[0]['wins'] == 0:
            return []
        top_record = (candidates[0]['wins'], candidates[0]['losses'])
        return [p for p in candidates if (p['wins'], p['losses']) == top_record]

    runouts_top = get_top_players(player_stats_data, stat_key='runs')
    eights_top = get_top_players(player_stats_data, stat_key='eights')
    sweeps_top = get_top_players(player_stats_data, stat_key='sweeps')
    awards = [
        {
            'label': 'Top Male',
            'amount': Decimal('100'),
            'players': get_top_by_record(player_stats_data, True),
        },
        {
            'label': 'Top Female',
            'amount': Decimal('100'),
            'players': get_top_by_record(player_stats_data, False),
        },
        {
            'label': 'Most Runouts',
            'amount': Decimal('20'),
            'players': runouts_top,
            'count': runouts_top[0]['runs'] if runouts_top else 0,
        },
        {
            'label': 'Most 8 on the Breaks',
            'amount': Decimal('20'),
            'players': eights_top,
            'count': eights_top[0]['eights'] if eights_top else 0,
        },
        {
            'label': 'Most 5/0s',
            'amount': Decimal('20'),
            'players': sweeps_top,
            'count': sweeps_top[0]['sweeps'] if sweeps_top else 0,
        },
    ]
    total_awards_amount = sum(award['amount'] for award in awards)

    # Current Payout Rate (based on money collected so far)
    total_games_won_so_far = sum(row['games_won'] for row in standings_data)
    current_payout_rate = Decimal('0')
    if total_games_won_so_far > 0:
        # User calculation: (Current Balance - Tournament - Awards) / Games Won So Far
        # For pool: (1860 - 300 - 260) / 775 = 1.677
        current_payout_rate = (current_balance - tournament_money - total_awards_amount) / Decimal(total_games_won_so_far)
        if current_payout_rate < 0:
            current_payout_rate = Decimal('0')

    # Total payout amount available for games won (Projected Full Season)
    total_payout_amount = total_weekly_payout_pool - tournament_money - total_awards_amount

    # Project total games for the season
    # For 8-ball, it's exactly team_size * team_size (25)
    # For one-pocket, it's variable. Use average games won per match so far.
    if total_matches_played > 0:
        average_games_per_match = Decimal(total_games_won_so_far) / Decimal(total_matches_played)
    else:
        average_games_per_match = Decimal(team_size * team_size)
    
    total_projected_games = average_games_per_match * Decimal(total_matches_in_season)

    projected_payout_rate = Decimal('0')
    if total_projected_games > 0:
        projected_payout_rate = total_payout_amount / total_projected_games

    matches_per_team = Decimal(total_matches_in_season) * Decimal('2') / Decimal(team_count)
    total_games_per_team_season = matches_per_team * average_games_per_match

    standings = []
    for row in standings_data:
        games_won = row['games_won']
        games_lost = row['games_lost']
        games_played = games_won + games_lost

        if games_played > 0:
            win_percent = Decimal(games_won) / Decimal(games_played)
        else:
            win_percent = Decimal('0.5')

        projected_games_won = win_percent * total_games_per_team_season
        projected_payout = projected_games_won * projected_payout_rate

        standings.append({
            'team': row['team'],
            'games_won': games_won,
            'payout': Decimal(games_won) * current_payout_rate,
            'projected_games_won': projected_games_won,
            'projected_payout': projected_payout,
        })

    total_current_payout = sum(item['payout'] for item in standings)
    total_games_won = sum(item['games_won'] for item in standings)
    total_projected_games_won = sum(item['projected_games_won'] for item in standings)
    total_projected_payout = sum(item['projected_payout'] for item in standings)

    return render(request, 'finance.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'team_count': team_count,
        'weeks_played': weeks_played,
        'total_matches_played': total_matches_played,
        'current_balance': current_balance,
        'total_payout_amount': total_payout_amount,
        'payout_rate': current_payout_rate,
        'projected_payout_rate': projected_payout_rate,
        'standings': standings,
        'total_current_payout': total_current_payout,
        'total_games_won': total_games_won,
        'total_projected_games_won': total_projected_games_won,
        'total_projected_payout': total_projected_payout,
        'awards': awards,
        'signup_fee': signup_fee,
        'fee_per_player': fee_per_player,
        'greens_fee': greens_fee,
        'operator_pay_per_player': operator_pay_per_player,
        'tournament_money': tournament_money,
        'total_awards_amount': total_awards_amount,
    })


def _top_n_with_record_ties(stats, n, record_keys=('wins', 'losses')):
    if not stats or n <= 0:
        return []

    def record_of(stat):
        return tuple(stat[key] for key in record_keys)

    result = list(stats[:n])
    if len(stats) > n:
        cutoff_record = record_of(stats[n - 1])
        for stat in stats[n:]:
            if record_of(stat) != cutoff_record:
                break
            result.append(stat)

    prev_record = None
    prev_rank = 0
    for index, stat in enumerate(result, start=1):
        record = record_of(stat)
        if record == prev_record:
            stat['display_rank'] = prev_rank
        else:
            stat['display_rank'] = index
            prev_record = record
            prev_rank = index
    return result


def home(request):
    logging.info(f'Home Page -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)
    today = timezone.localdate()

    news_items = NewsItem.objects.none()
    current_schedule = []
    current_schedule_week = None
    current_schedule_is_holiday = False
    current_schedule_holiday_note = ''
    team_standings = []
    top_male_players = []
    top_female_players = []
    one_pocket_race_rows_left = []
    one_pocket_race_rows_right = []

    active_season = get_active_season(active_league)

    if active_league:
        news_items = NewsItem.objects.filter(
            league=active_league,
            show_date__lte=today,
        ).filter(
            Q(expiration_date__isnull=True) | Q(expiration_date__gte=today)
        )

        if active_league.results_type == League.ResultsType.ONE_POCKET:
            midpoint = (len(ONE_POCKET_RACE_ROWS) + 1) // 2
            one_pocket_race_rows_left = ONE_POCKET_RACE_ROWS[:midpoint]
            one_pocket_race_rows_right = ONE_POCKET_RACE_ROWS[midpoint:]

        team_standings = build_team_standings(active_league, active_season)

        if active_season:
            next_week = (
                Week.objects.filter(
                    season=active_season,
                    date__gte=today,
                )
                .prefetch_related(
                    Prefetch(
                        'matches',
                        queryset=Match.objects.select_related(
                            'home_team',
                            'away_team',
                            'result',
                        ).order_by('sort_order', 'id'),
                    )
                )
                .order_by('date')
                .first()
            )

            if next_week:
                current_schedule_week = next_week
                current_schedule_is_holiday = next_week.number is None
                current_schedule_holiday_note = (next_week.notes or '').strip()

                if not current_schedule_is_holiday:
                    current_schedule = build_week_schedule_with_byes(active_league, next_week, team_standings=team_standings)

        all_player_stats = build_player_stats(active_league, active_season)
        is_darts = active_league.results_type == League.ResultsType.DARTS

        if is_darts:
            # Darts ranks everyone together by points, not split by gender.
            top_male_players = _top_n_with_record_ties(
                [stat for stat in all_player_stats if stat['games_played'] > 0],
                5,
                record_keys=('points',),
            )
            top_female_players = []
        else:
            top_male_players = _top_n_with_record_ties(
                [stat for stat in all_player_stats if stat['male'] and stat['games_played'] > 0],
                5,
            )
            top_female_players = _top_n_with_record_ties(
                [stat for stat in all_player_stats if not stat['male'] and stat['games_played'] > 0],
                5,
            )

    has_archived_seasons = bool(active_league and active_league.archived_seasons.exists())

    return render(request, 'home.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'news_items': news_items,
        'current_schedule': current_schedule,
        'current_schedule_week': current_schedule_week,
        'current_schedule_is_holiday': current_schedule_is_holiday,
        'current_schedule_holiday_note': current_schedule_holiday_note,
        'top_male_players': top_male_players,
        'top_female_players': top_female_players,
        'team_standings': team_standings,
        'one_pocket_race_rows_left': one_pocket_race_rows_left,
        'one_pocket_race_rows_right': one_pocket_race_rows_right,
        'has_archived_seasons': has_archived_seasons,
    })

def schedule(request):
    logging.info(f'Schedule -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    schedule_weeks = []

    if active_season and active_league:
        team_standings = build_team_standings(active_league, active_season)
        weeks = (
            Week.objects.filter(season=active_season)
            .prefetch_related(
                Prefetch(
                    'matches',
                    queryset=Match.objects.select_related(
                        'home_team',
                        'away_team',
                        'result',
                    ).order_by('sort_order', 'id'),
                )
            )
            .order_by('date', 'number')
        )

        schedule_weeks = [
            {
                'week': week,
                'entries': build_week_schedule_with_byes(active_league, week, team_standings=team_standings),
            }
            for week in weeks
        ]

    return render(request, 'schedule.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'schedule_weeks': schedule_weeks,
    })


def standings(request):
    logging.info(f'Standings -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    available_weeks = Week.objects.none()
    selected_week = None
    team_standings = []

    if active_season:
        available_weeks = get_result_weeks(active_season)

        selected_week_id = request.GET.get('week')
        if selected_week_id:
            selected_week = available_weeks.filter(pk=selected_week_id).first()

        if selected_week is None:
            selected_week = available_weeks.last()

    if active_league:
        team_standings = build_team_standings(
            active_league,
            active_season,
            through_week=selected_week,
        )

    return render(request, 'standings.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'available_weeks': available_weeks,
        'selected_week': selected_week,
        'team_standings': team_standings,
    })


def player_stats(request):
    logging.info(f'Player Status -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    available_weeks = Week.objects.none()
    selected_week = None
    player_stats_data = []

    if active_season:
        available_weeks = get_result_weeks(active_season)

        selected_week_id = request.GET.get('week')
        if selected_week_id:
            selected_week = available_weeks.filter(pk=selected_week_id).first()

        if selected_week is None:
            selected_week = available_weeks.last()

    gender = request.GET.get('gender', 'all')
    sort_col = request.GET.get('sort', 'wins')
    sort_dir = request.GET.get('dir', 'desc')

    if active_league:
        player_stats_data = build_player_stats(
            active_league,
            active_season,
            through_week=selected_week,
        )

        if gender == 'male':
            player_stats_data = [p for p in player_stats_data if p['male']]
        elif gender == 'female':
            player_stats_data = [p for p in player_stats_data if not p['male']]

        valid_sort_cols = {
            'player': 'player',
            'team': 'team',
            'wins': 'wins',
            'losses': 'losses',
            'percentage': 'percentage',
            'runs': 'runs',
            'sweeps': 'sweeps',
            'eights': 'eights',
        }

        if sort_col in valid_sort_cols:
            key_field = valid_sort_cols[sort_col]
            reverse = (sort_dir == 'desc')

            if key_field == 'player':
                player_stats_data.sort(key=lambda x: x['player'].lower(), reverse=reverse)
            elif key_field == 'team':
                player_stats_data.sort(key=lambda x: x['team'].lower(), reverse=reverse)
            else:
                player_stats_data.sort(key=lambda x: x[key_field], reverse=reverse)

    return render(request, 'player_stats.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'available_weeks': available_weeks,
        'selected_week': selected_week,
        'player_stats_data': player_stats_data,
        'selected_gender': gender,
        'sort_col': sort_col,
        'sort_dir': sort_dir,
    })

def contact_info(request):
    logging.info(f'Contact Info -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)

    teams = Team.objects.none()
    if active_league:
        teams = (
            Team.objects.filter(league=active_league)
            .select_related('captain')
            .order_by('name')
        )

    return render(request, 'contact_info.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'teams': teams,
    })

def rules(request):
    logging.info(f'Rules -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)

    raw_rules = Rule.objects.none()
    rule_blocks = []

    if active_league:
        raw_rules = Rule.objects.filter(
            league=active_league,
        ).order_by('order', 'id')

        current_entries = []

        for rule in raw_rules:
            if rule.rule_type == Rule.RuleType.RULE_ENTRY:
                current_entries.append(rule.text)
            else:
                if current_entries:
                    rule_blocks.append({
                        'type': 'rule_entries',
                        'entries': current_entries,
                    })
                    current_entries = []

                rule_blocks.append({
                    'type': rule.rule_type,
                    'text': rule.text,
                })

        if current_entries:
            rule_blocks.append({
                'type': 'rule_entries',
                'entries': current_entries,
            })

    return render(request, 'rules.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'rule_blocks': rule_blocks,
    })

# ... existing code ...
def team_detail(request, team_id):
    logging.info(f'Team Detail -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    team = get_object_or_404(
        Team.objects.select_related('league', 'captain', 'venue'),
        pk=team_id,
    )

    league_rank = None
    matches_won = 0
    matches_lost = 0
    games_won = 0
    games_lost = 0

    if active_season and team.league_id == active_season.league_id:
        standings = build_team_standings(team.league, active_season)
        for index, standing in enumerate(standings, start=1):
            if standing['team'] == team.name:
                league_rank = index
                matches_won = standing['matches_won']
                matches_lost = standing['matches_lost']
                games_won = standing['games_won']
                games_lost = standing['games_lost']
                break

    team_player_stats = build_team_player_stats(active_season, team)

    team_schedule = []
    if active_season and team.league_id == active_season.league_id:
        season_weeks = (
            Week.objects.filter(
                season=active_season,
            )            .prefetch_related(
                Prefetch(
                    'matches',
                    queryset=Match.objects.select_related(
                        'home_team',
                        'away_team',
                        'week',
                    ).order_by('sort_order', 'id'),
                )
            )
            .order_by('date')
        )

        for week in season_weeks:
            is_holiday_week = week.number is None
            week_match = None if is_holiday_week else week.matches.filter(
                Q(home_team=team) | Q(away_team=team)
            ).first()

            if week_match:
                is_home = week_match.home_team_id == team.id
                opponent = week_match.away_team if is_home else week_match.home_team

                result_label = ''
                match_detail_rows = []

                if hasattr(week_match, 'result'):
                    if team.league.results_type in (League.ResultsType.ONE_POCKET, League.ResultsType.DARTS):
                        home_games_won = week_match.result.home_team_score or 0
                        away_games_won = week_match.result.away_team_score or 0
                    else:
                        home_games_won = 0
                        away_games_won = 0

                        for player_result in week_match.result.player_results.select_related('player', 'represented_team').all():
                            if player_result.represented_team_id == week_match.home_team_id:
                                home_games_won += player_result.wins
                            elif player_result.represented_team_id == week_match.away_team_id:
                                away_games_won += player_result.wins

                    for player_result in week_match.result.player_results.select_related('player', 'represented_team').all():
                        is_home_row = player_result.represented_team_id == week_match.home_team_id
                        match_detail_rows.append({
                            'player': player_result.player.name,
                            'represented_team': player_result.represented_team.name,
                            'is_home': is_home_row,
                            'wins': player_result.wins,
                            'losses': player_result.losses,
                            'runouts': player_result.runouts,
                            'eights': player_result.eight_on_the_breaks,
                            'sweeps': player_result.won_all_games,
                            'hat_tricks': player_result.hat_tricks,
                            'three_in_a_beds': player_result.three_in_a_beds,
                            'white_horses': player_result.white_horses,
                            'three_in_the_blacks': player_result.three_in_the_blacks,
                            'points': player_result.darts_points,
                        })

                    match_detail_rows.sort(key=lambda r: (0 if r['is_home'] else 1, r['player']))

                    team_games_won = home_games_won if is_home else away_games_won
                    opponent_games_won = away_games_won if is_home else home_games_won
                    if (
                        team.league.results_type == League.ResultsType.ONE_POCKET
                        and max(home_games_won, away_games_won) < 3
                    ):
                        result_label = ''
                    else:
                        result_label = f'{team_games_won}-{opponent_games_won}'

                team_schedule.append({
                    'match_id': week_match.id,
                    'week': week,
                    'is_home': is_home,
                    'opponent': opponent,
                    'location': week_match.location,
                    'result_label': result_label,
                    'match_detail_rows': match_detail_rows,
                    'is_bye': False,
                    'is_holiday': False,
                })
            else:
                team_schedule.append({
                    'match_id': None,
                    'week': week,
                    'is_home': False,
                    'opponent': 'Bye',
                    'location': '',
                    'result_label': '',
                    'match_detail_rows': [],
                    'is_bye': True,
                    'is_holiday': is_holiday_week,
                })

    return render(request, 'team_detail.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'team': team,
        'league_rank': league_rank,
        'matches_won': matches_won,
        'matches_lost': matches_lost,
        'games_won': games_won,
        'games_lost': games_lost,
        'team_player_stats': team_player_stats,
        'team_schedule': team_schedule,
    })

def build_player_vs_team_stats(player, active_season, through_week=None):
    results = (
        PlayerMatchResult.objects.filter(
            player=player,
            match_result__match__week__season=active_season,
        )
        .select_related(
            'represented_team',
            'match_result__match__home_team',
            'match_result__match__away_team',
            'match_result__match__week',
        )
        .order_by('match_result__match__week__date', 'match_result__match__sort_order', 'id')
    )

    if through_week is not None:
        results = results.filter(
            match_result__match__week__date__lte=through_week.date,
        )

    rows = []
    for player_result in results:
        match = player_result.match_result.match

        if player_result.represented_team_id == match.home_team_id:
            opponent = match.away_team
        else:
            opponent = match.home_team

        rows.append({
            'represented_team_id': player_result.represented_team_id,
            'represented_team_name': player_result.represented_team.name,
            'team_id': opponent.id,
            'team_name': opponent.name,
            'week_number': match.week.number,
            'week_date': match.week.date,
            'wins': player_result.wins,
            'losses': player_result.losses,
            'runs': player_result.runouts,
            'sweeps': 1 if player_result.won_all_games else 0,
            'eights': player_result.eight_on_the_breaks,
            'hat_tricks': player_result.hat_tricks,
            'three_in_a_beds': player_result.three_in_a_beds,
            'white_horses': player_result.white_horses,
            'three_in_the_blacks': player_result.three_in_the_blacks,
            'points': player_result.darts_points,
        })

    return sorted(rows, key=lambda row: row['week_date'])


def player_scores_modal(request, player_id):
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    if not active_league:
        raise Http404('No active league found.')

    player = get_object_or_404(
        Player.objects.select_related('team', 'league'),
        pk=player_id,
        league=active_league,
    )

    through_week = None
    if active_season:
        selected_week_id = request.GET.get('week')
        if selected_week_id:
            through_week = Week.objects.filter(
                pk=selected_week_id,
                season=active_season,
            ).first()

    matchup_rows = []
    if active_season:
        matchup_rows = build_player_vs_team_stats(
            player=player,
            active_season=active_season,
            through_week=through_week,
        )

    is_darts = active_league.results_type == League.ResultsType.DARTS
    season_history = []
    career_totals = None
    if not is_darts:
        from scheduling.services import build_player_season_history

        season_history, career_totals = build_player_season_history(player)

    html = render_to_string(
        'player_scores_modal.html',
        {
            'player': player,
            'active_season': active_season,
            'through_week': through_week,
            'matchup_rows': matchup_rows,
            'is_darts': is_darts,
            'season_history': season_history,
            'career_totals': career_totals,
            'team_size': active_league.team_size,
        },
        request=request,
    )

    return JsonResponse({'html': html})


def team_schedule_modal(request, team_id):
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    if not active_league or active_league.results_type != League.ResultsType.ONE_POCKET:
        raise Http404('Team schedule modal is only available for one pocket leagues.')

    team = get_object_or_404(
        Team.objects.select_related('league'),
        pk=team_id,
        league=active_league,
    )

    results_rows = []
    makeup_rows = []
    upcoming_rows = []
    today = date.today()
    current_player = MatchResult.slot_player(team)
    current_player_id = current_player.id if current_player else None

    if active_season:
        team_standings = build_team_standings(active_league, active_season)
        rank_map = {s['team_id']: s['league_rank'] for s in team_standings}

        matches = (
            Match.objects.filter(
                week__season=active_season,
            )
            .filter(
                Q(home_team=team) | Q(away_team=team)
            )
            .select_related('home_team', 'away_team', 'week', 'result')
            .order_by('week__date', 'sort_order', 'id')
        )

        for match in matches:
            is_home = match.home_team_id == team.id
            opponent = match.away_team if is_home else match.home_team

            if hasattr(match, 'result'):
                home_score = match.result.home_team_score or 0
                away_score = match.result.away_team_score or 0
                is_complete = max(home_score, away_score) >= ONE_POCKET_COMPLETE_SCORE

                if is_complete:
                    team_score = home_score if is_home else away_score
                    opponent_score = away_score if is_home else home_score
                    played_by = match.result.side_player(team)
                    played_by_name = (
                        played_by.name if played_by is not None and played_by.id != current_player_id else ''
                    )

                    results_rows.append({
                        'played_by': played_by_name,
                        'date': match.week.date,
                        # When the score went in: a makeup played early or
                        # late shows a different date here than the week.
                        'entered': timezone.localtime(match.result.created_at).date(),
                        'opponent': opponent.name,
                        # Ranks as they stood when the match was played.
                        'opponent_rank': match.result.side_rank(opponent),
                        'my_rank': match.result.side_rank(team),
                        'result': f'{team_score}-{opponent_score}',
                        'won': team_score > opponent_score,
                    })
                else:
                    row = {
                        'date': match.week.date,
                        'opponent': opponent.name,
                        'opponent_rank': opponent.team_rank,
                    }
                    if match.week.date < today:
                        makeup_rows.append(row)
                    else:
                        upcoming_rows.append(row)
            else:
                opponent_label = opponent.name

                row = {
                    'date': match.week.date,
                    'opponent': opponent_label,
                    'opponent_rank': opponent.team_rank,
                }

                if match.week.date < today:
                    makeup_rows.append(row)
                else:
                    upcoming_rows.append(row)

    # Headline numbers are for the player currently in the spot; matches
    # played by someone who held the spot earlier are listed but not counted.
    own_rows = [r for r in results_rows if not r['played_by']]
    wins = sum(1 for r in own_rows if r['won'])
    losses = len(own_rows) - wins
    pct = (wins / len(own_rows)) if own_rows else None
    ring_circumference = 2 * 3.14159 * 45  # r=45 in the SVG below
    overall = {
        'wins': wins,
        'losses': losses,
        'pct': pct,
        'pct_label': f'{round(pct * 100)}%' if pct is not None else '-',
        # stroke-dasharray "filled gap" draws the ring to the percentage
        'ring_filled': round(ring_circumference * (pct or 0), 1),
        'ring_total': round(ring_circumference, 1),
    }

    rank_values = sorted({r['opponent_rank'] for r in own_rows if r['opponent_rank']} | set(range(1, 6)), reverse=True)
    by_rank = []
    for rank in rank_values:
        rows = [r for r in own_rows if r['opponent_rank'] == rank]
        rank_wins = sum(1 for r in rows if r['won'])
        by_rank.append({
            'rank': rank,
            'wins': rank_wins,
            'losses': len(rows) - rank_wins,
            'played': len(rows),
            'pct_label': f'{round(rank_wins / len(rows) * 100)}%' if rows else '-',
        })

    from .player_page import build_player_extras
    from .rank_review import player_outlook

    extras = build_player_extras(current_player, team, own_rows, results_rows, upcoming_rows)
    rank_outlook = (
        player_outlook(active_league, active_season, current_player.id) if current_player else None
    )

    html = render_to_string(
        'team_schedule_modal.html',
        {
            'team': team,
            'team_league_rank': rank_map.get(team.id) if active_season else None,
            'team_skill_rank': team.team_rank,
            'overall': overall,
            'by_rank': by_rank,
            'rank_outlook': rank_outlook,
            **extras,
            'results_rows': results_rows,
            'former_players': sorted({r['played_by'] for r in results_rows if r['played_by']}),
            'makeup_rows': makeup_rows,
            'upcoming_rows': upcoming_rows,
        },
        request=request,
    )

    return JsonResponse({'html': html})


def one_pocket_full_schedule_modal(request):
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    if not active_league or active_league.results_type != League.ResultsType.ONE_POCKET:
        raise Http404('Full schedule modal is only available for one pocket leagues.')

    schedule_weeks = []

    if active_season:
        team_standings = build_team_standings(active_league, active_season)
        weeks = (
            Week.objects.filter(season=active_season)
            .prefetch_related(
                Prefetch(
                    'matches',
                    queryset=Match.objects.select_related('home_team', 'away_team', 'result').order_by('sort_order', 'id'),
                )
            )
            .order_by('date', 'number')
        )

        schedule_weeks = [
            {
                'week': week,
                'entries': build_week_schedule_with_byes(active_league, week, team_standings=team_standings),
            }
            for week in weeks
        ]

    html = render_to_string(
        'team_full_schedule_modal.html',
        {
            'schedule_weeks': schedule_weeks,
        },
        request=request,
    )
    return JsonResponse({'html': html})

def league_supports_archives(league):
    """Darts leagues don't use the archive pages; pool leagues do."""
    return bool(league) and league.results_type != League.ResultsType.DARTS


def get_archived_season_options(active_league, selected_id=None):
    """Return (options, selected) for the active league's archives.

    Archives are always scoped to the active league so one site can never
    display another league's seasons.
    """
    archived_seasons_qs = active_league.archived_seasons.prefetch_related(
        'teams',
        'players',
    ).order_by('-archived_at', '-id')

    options = list(archived_seasons_qs)

    selected = None
    if selected_id:
        selected = archived_seasons_qs.filter(pk=selected_id).first()
    if selected is None and options:
        selected = options[0]

    return options, selected


def archived_season_results_type(archived_season):
    # Archives created before the snapshot fields existed fall back to the
    # league's current type (the backfill migration sets it, but tests may not).
    if archived_season.results_type:
        return archived_season.results_type
    return archived_season.league.results_type


def build_archived_standings(archived_season):
    if archived_season is None:
        return [], []

    team_standings = [
        {
            'team_name': team.team_name,
            'matches_won': team.matches_won,
            'matches_lost': team.matches_lost,
            'games_won': team.games_won,
            'games_lost': team.games_lost,
            'league_rank': team.league_rank,
            'team_rank': team.team_rank,
            'captain_name': team.captain_name,
            'venue_name': team.venue_name,
        }
        for team in archived_season.teams.all()
    ]
    team_standings.sort(
        key=lambda row: (
            row['league_rank'] if row['league_rank'] is not None else float('inf'),
            -row['matches_won'],
            row['matches_lost'],
            -row['games_won'],
            row['games_lost'],
            row['team_name'],
        )
    )

    player_standings = [
        {
            'player_name': player.player_name,
            'team_name': player.team_name,
            'games_won': player.games_won,
            'games_lost': player.games_lost,
            'run_outs': player.run_outs,
            'eight_on_the_breaks': player.eight_on_the_breaks,
            'sweeps': player.sweeps,
            'male': player.male,
        }
        for player in archived_season.players.all()
    ]
    player_standings.sort(
        key=lambda row: (-row['games_won'], -row['sweeps'], row['player_name'])
    )

    return team_standings, player_standings


def build_archived_match_weeks(archived_season):
    """Group an archived season's matches by date for display."""
    if archived_season is None:
        return []

    weeks = []
    current = None
    for match in archived_season.matches.all().order_by('date', 'sort_order', 'id'):
        if current is None or current['date'] != match.date:
            current = {
                'date': match.date,
                'week_number': match.week_number,
                'matches': [],
            }
            weeks.append(current)
        current['matches'].append(match)
    return weeks


def archived_seasons(request):
    logging.info(f'Archived Seasons -> active league: {get_active_league(request)}, ip address: {get_client_ip(request)}, host:{request.headers["Host"]}, user-agent: {request.headers["User-Agent"]}, method: {request.method}, path: {request.path}')
    active_league = get_active_league(request)

    archives_supported = league_supports_archives(active_league)
    archived_season_options = []
    selected_archived_season = None

    if archives_supported:
        archived_season_options, selected_archived_season = get_archived_season_options(
            active_league,
            request.GET.get('season'),
        )

    archived_team_standings, archived_player_standings = build_archived_standings(
        selected_archived_season
    )
    archived_match_weeks = build_archived_match_weeks(selected_archived_season)

    archive_results_type = (
        archived_season_results_type(selected_archived_season)
        if selected_archived_season
        else (active_league.results_type if active_league else '')
    )

    return render(request, 'archived_seasons.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'archives_supported': archives_supported,
        'archived_season_options': archived_season_options,
        'selected_archived_season': selected_archived_season,
        'is_one_pocket_archive': archive_results_type == League.ResultsType.ONE_POCKET,
        'archived_team_standings': archived_team_standings,
        'archived_player_standings': archived_player_standings,
        'archived_match_weeks': archived_match_weeks,
        'has_archived_matches': bool(archived_match_weeks),
    })


def archived_standings_modal(request):
    active_league = get_active_league(request)
    if not league_supports_archives(active_league):
        raise Http404('No active league found.')

    archived_season_options, selected_archived_season = get_archived_season_options(
        active_league,
        request.GET.get('season'),
    )
    if not archived_season_options:
        raise Http404('No archived seasons available.')

    archived_team_standings, archived_player_standings = build_archived_standings(
        selected_archived_season
    )

    html = render_to_string(
        'archived_standings_modal.html',
        {
            'active_league': active_league,
            'archived_season_options': archived_season_options,
            'selected_archived_season': selected_archived_season,
            'is_one_pocket_archive': (
                archived_season_results_type(selected_archived_season) == League.ResultsType.ONE_POCKET
            ),
            'archived_team_standings': archived_team_standings,
            'archived_player_standings': archived_player_standings,
        },
        request=request,
    )

    return JsonResponse({'html': html})


def archived_player_history(request, archived_season_id, player_name):
    active_league = get_active_league(request)
    if not league_supports_archives(active_league):
        raise Http404('No active league found.')

    # Scoped to the active league: a Bogies URL can never pull EMC rows.
    archived_season = get_object_or_404(
        ArchivedSeason,
        pk=archived_season_id,
        league=active_league,
    )
    is_one_pocket = archived_season_results_type(archived_season) == League.ResultsType.ONE_POCKET

    player_results = ArchivedPlayerMatchResult.objects.filter(
        archived_match__archived_season=archived_season,
        player_name=player_name
    ).select_related('archived_match').order_by('archived_match__date', 'archived_match__sort_order')

    history = []
    for pr in player_results:
        match = pr.archived_match
        is_home = pr.team_name == match.home_team_name

        opponent_names = match.away_team_name if is_home else match.home_team_name

        if is_home:
            team_score = match.home_team_score
            opp_score = match.away_team_score
            my_rank, opp_rank = match.home_team_rank, match.away_team_rank
            my_race, opp_race = match.home_race, match.away_race
        else:
            team_score = match.away_team_score
            opp_score = match.home_team_score
            my_rank, opp_rank = match.away_team_rank, match.home_team_rank
            my_race, opp_race = match.away_race, match.home_race

        if team_score is None or opp_score is None or not match.completed:
            outcome = ''
        elif team_score > opp_score:
            outcome = 'W'
        elif team_score < opp_score:
            outcome = 'L'
        else:
            outcome = 'T'

        history.append({
            'date': match.date,
            'entered': (
                timezone.localtime(match.result_recorded_at).date()
                if match.result_recorded_at else None
            ),
            'week_number': match.week_number,
            'opponent': opponent_names,
            'team_score': team_score,
            'opp_score': opp_score,
            'outcome': outcome,
            'wins': pr.wins,
            'losses': pr.losses,
            'runouts': pr.runouts,
            'eight_on_the_breaks': pr.eight_on_the_breaks,
            'won_all_games': pr.won_all_games,
            'my_rank': my_rank,
            'opp_rank': opp_rank,
            'race_label': f'{my_race}/{opp_race}' if my_race is not None and opp_race is not None else '',
        })

    html = render_to_string('archived_player_history_modal.html', {
        'player_name': player_name,
        'season_name': archived_season.name,
        'is_one_pocket': is_one_pocket,
        'team_size': archived_season.team_size or archived_season.league.team_size,
        'history': history,
    }, request=request)
    return JsonResponse({'html': html})

def build_team_player_stats(active_season, team, through_week=None):
    is_darts = team.league.results_type == League.ResultsType.DARTS

    player_map = {
        player.id: {
            'player_id': player.id,
            'player': player.name,
            'team': team.name,
            'male': player.male,
            'wins': 0,
            'losses': 0,
            'percentage': 0.0,
            'runs': 0,
            'sweeps': 0,
            'eights': 0,
            'hat_tricks': 0,
            'three_in_a_beds': 0,
            'white_horses': 0,
            'three_in_the_blacks': 0,
            'points': 0,
            'tie_breaker': 0,
        }
        for player in team.players.all()
    }

    if active_season is None:
        # Between seasons (e.g. just archived): show the roster, zero-filled.
        results = PlayerMatchResult.objects.none()
    else:
        results = (
            PlayerMatchResult.objects.filter(
                match_result__match__week__season=active_season,
                represented_team=team,
            )
            .select_related(
                'player',
                'represented_team',
                'match_result__match__week',
            )
            .order_by('player__name', 'match_result__match__week__date', 'match_result__match__sort_order', 'id')
        )

    if through_week is not None:
        results = results.filter(
            match_result__match__week__date__lte=through_week.date,
        )

    for player_result in results:
        player_id = player_result.player_id

        if player_id not in player_map:
            player_map[player_id] = {
                'player_id': player_id,
                'player': player_result.player.name,
                'team': team.name,
                'male': player_result.player.male,
                'wins': 0,
                'losses': 0,
                'percentage': 0.0,
                'runs': 0,
                'sweeps': 0,
                'eights': 0,
                'hat_tricks': 0,
                'three_in_a_beds': 0,
                'white_horses': 0,
                'three_in_the_blacks': 0,
                'points': 0,
                'tie_breaker': 0,
            }

        player_map[player_id]['wins'] += player_result.wins
        player_map[player_id]['losses'] += player_result.losses
        player_map[player_id]['runs'] += player_result.runouts
        player_map[player_id]['sweeps'] += 1 if player_result.won_all_games else 0
        player_map[player_id]['eights'] += player_result.eight_on_the_breaks
        player_map[player_id]['hat_tricks'] += player_result.hat_tricks
        player_map[player_id]['three_in_a_beds'] += player_result.three_in_a_beds
        player_map[player_id]['white_horses'] += player_result.white_horses
        player_map[player_id]['three_in_the_blacks'] += player_result.three_in_the_blacks
        player_map[player_id]['points'] += player_result.darts_points

    for stat in player_map.values():
        stat['tie_breaker'] = stat['sweeps'] + stat['eights'] + stat['runs']
        total = stat['wins'] + stat['losses']
        stat['percentage'] = ((stat['wins'] / total) * 100) if total > 0 else 0.0

    if is_darts:
        def sort_key(stat):
            return (
                -stat['points'],
                -stat['hat_tricks'],
                -stat['three_in_a_beds'],
                -stat['white_horses'],
                -stat['three_in_the_blacks'],
                stat['player'],
            )
    else:
        def sort_key(stat):
            return (
                -stat['wins'],
                -stat['tie_breaker'],
                stat['player'],
            )

    return sorted(player_map.values(), key=sort_key)
