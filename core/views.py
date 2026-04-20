from datetime import date

from django.conf import settings
from django.db.models import Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone

from content.models import NewsItem, Rule
from results.models import PlayerMatchResult
from scheduling.models import Match, Season, Week
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


def get_active_league(request):
    requested_league_id = request.GET.get('league')
    if requested_league_id:
        request.session['frontend_league_id'] = requested_league_id

    league_id = (
        request.session.get('frontend_league_id')
        or settings.FRONTEND_LEAGUE_ID
    )

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


def get_one_pocket_race_label(team_a, team_b):
    rank_a = team_a.team_rank
    rank_b = team_b.team_rank

    if rank_a is None or rank_b is None:
        return ''

    higher_rank = max(rank_a, rank_b)
    lower_rank = min(rank_a, rank_b)

    race_lookup = {
        (row['higher_rank'], row['lower_rank']): (row['higher_race'], row['lower_race'])
        for row in ONE_POCKET_RACE_ROWS
    }

    race = race_lookup.get((higher_rank, lower_rank))
    if not race:
        return ''

    return f'{race[0]}/{race[1]}'


def build_week_schedule_with_byes(active_league, week):
    teams = list(
        Team.objects.filter(league=active_league).order_by('name')
    )
    matches = list(
        week.matches.all()
    )

    scheduled_team_ids = set()
    for match in matches:
        scheduled_team_ids.add(match.home_team_id)
        scheduled_team_ids.add(match.away_team_id)

    bye_entries = []
    for team in teams:
        if team.id not in scheduled_team_ids:
            bye_entries.append({
                'home_team': team,
                'away_team': 'BYE',
                'location': '',
                'is_bye': True,
                'race_label': '',
            })

    match_entries = [
        {
            'home_team': match.home_team,
            'away_team': match.away_team,
            'location': match.location,
            'is_bye': False,
            'race_label': (
                get_one_pocket_race_label(match.home_team, match.away_team)
                if active_league.results_type == League.ResultsType.ONE_POCKET
                else ''
            ),
        }
        for match in matches
    ]

    return match_entries + bye_entries


def build_team_standings(active_league, active_season, through_week=None):
    teams = list(
        Team.objects.filter(league=active_league)
        .order_by('name')
    )

    standings_map = {
        team.id: {
            'team_id': team.id,
            'team': team.name,
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
                -standing['games_won'],
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

        if active_league.results_type == League.ResultsType.ONE_POCKET:
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

        standings_map[match.home_team_id]['games_won'] += home_games_won
        standings_map[match.home_team_id]['games_lost'] += home_games_lost

        standings_map[match.away_team_id]['games_won'] += away_games_won
        standings_map[match.away_team_id]['games_lost'] += away_games_lost

        if active_league.results_type == League.ResultsType.ONE_POCKET:
            if home_games_won > away_games_won:
                standings_map[match.home_team_id]['matches_won'] += 1
                standings_map[match.away_team_id]['matches_lost'] += 1
            elif away_games_won > home_games_won:
                standings_map[match.away_team_id]['matches_won'] += 1
                standings_map[match.home_team_id]['matches_lost'] += 1
        else:
            if home_games_won > match_win_threshold:
                standings_map[match.home_team_id]['matches_won'] += 1
                standings_map[match.away_team_id]['matches_lost'] += 1
            elif away_games_won > match_win_threshold:
                standings_map[match.away_team_id]['matches_won'] += 1
                standings_map[match.home_team_id]['matches_lost'] += 1

    return sorted(
        standings_map.values(),
        key=lambda standing: (
            -standing['matches_won'],
            -standing['games_won'],
            standing['team'],
        ),
    )


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
            'tie_breaker': 0,
        }
        for player in players
    }

    if not active_season:
        return sorted(
            stats_map.values(),
            key=lambda stat: (
                -stat['wins'],
                -stat['tie_breaker'],
                stat['player'],
            ),
        )

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

    for stat in stats_map.values():
        stat['tie_breaker'] = stat['sweeps'] + stat['eights'] + stat['runs']
        total = stat['wins'] + stat['losses']
        stat['percentage'] = ((stat['wins'] / total) * 100) if total > 0 else 0.0

    return sorted(
        stats_map.values(),
        key=lambda stat: (
            -stat['wins'],
            -stat['tie_breaker'],
            stat['player'],
        ),
    )


def home(request):
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
                    current_schedule = build_week_schedule_with_byes(active_league, next_week)

        team_standings = build_team_standings(active_league, active_season)

        all_player_stats = build_player_stats(active_league, active_season)
        top_male_players = [stat for stat in all_player_stats if stat['male'] and (stat['wins'] + stat['losses']) > 0][:5]
        top_female_players = [stat for stat in all_player_stats if not stat['male'] and (stat['wins'] + stat['losses']) > 0][:5]

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
    })

def schedule(request):
    active_league = get_active_league(request)
    active_season = get_active_season(active_league)

    schedule_weeks = []

    if active_season and active_league:
        weeks = (
            Week.objects.filter(season=active_season)
            .prefetch_related(
                Prefetch(
                    'matches',
                    queryset=Match.objects.select_related(
                        'home_team',
                        'away_team',
                    ).order_by('sort_order', 'id'),
                )
            )
            .order_by('date', 'number')
        )

        schedule_weeks = [
            {
                'week': week,
                'entries': build_week_schedule_with_byes(active_league, week),
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

    if active_league:
        player_stats_data = build_player_stats(
            active_league,
            active_season,
            through_week=selected_week,
        )

    return render(request, 'player_stats.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'available_weeks': available_weeks,
        'selected_week': selected_week,
        'player_stats_data': player_stats_data,
    })

def contact_info(request):
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

    all_player_stats = build_player_stats(team.league, active_season)
    team_player_stats = [
        stat for stat in all_player_stats
        if stat['team'] == team.name
    ]

    team_schedule = []
    if active_season:
        monday_weeks = (
            Week.objects.filter(
                season=active_season,
                date__week_day=2,
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

        for week in monday_weeks:
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
                    home_games_won = 0
                    away_games_won = 0

                    for player_result in week_match.result.player_results.all():
                        if player_result.represented_team_id == week_match.home_team_id:
                            home_games_won += player_result.wins
                        elif player_result.represented_team_id == week_match.away_team_id:
                            away_games_won += player_result.wins

                        match_detail_rows.append({
                            'player': player_result.player.name,
                            'represented_team': player_result.represented_team.name,
                            'wins': player_result.wins,
                            'losses': player_result.losses,
                            'runouts': player_result.runouts,
                            'eights': player_result.eight_on_the_breaks,
                            'sweeps': player_result.won_all_games,
                        })

                    team_games_won = home_games_won if is_home else away_games_won
                    opponent_games_won = away_games_won if is_home else home_games_won
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

    team_map = {}

    for player_result in results:
        match = player_result.match_result.match

        if player_result.represented_team_id == match.home_team_id:
            opponent = match.away_team
        else:
            opponent = match.home_team

        if opponent.id not in team_map:
            team_map[opponent.id] = {
                'team_id': opponent.id,
                'team_name': opponent.name,
                'wins': 0,
                'losses': 0,
                'runs': 0,
                'sweeps': 0,
                'eights': 0,
            }

        team_map[opponent.id]['wins'] += player_result.wins
        team_map[opponent.id]['losses'] += player_result.losses
        team_map[opponent.id]['runs'] += player_result.runouts
        team_map[opponent.id]['sweeps'] += 1 if player_result.won_all_games else 0
        team_map[opponent.id]['eights'] += player_result.eight_on_the_breaks

    return sorted(team_map.values(), key=lambda row: row['team_name'])


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

    html = render_to_string(
        'player_scores_modal.html',
        {
            'player': player,
            'active_season': active_season,
            'through_week': through_week,
            'matchup_rows': matchup_rows,
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

    if active_season:
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
                team_score = (
                    (match.result.home_team_score or 0)
                    if is_home else
                    (match.result.away_team_score or 0)
                )
                opponent_score = (
                    (match.result.away_team_score or 0)
                    if is_home else
                    (match.result.home_team_score or 0)
                )

                results_rows.append({
                    'date': match.week.date,
                    'opponent': opponent.name,
                    'result': f'{team_score}-{opponent_score}',
                })
            else:
                row = {
                    'date': match.week.date,
                    'opponent': opponent.name,
                }

                if match.week.date < today:
                    makeup_rows.append(row)
                else:
                    upcoming_rows.append(row)

    html = render_to_string(
        'team_schedule_modal.html',
        {
            'team': team,
            'results_rows': results_rows,
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
        weeks = (
            Week.objects.filter(season=active_season)
            .prefetch_related(
                Prefetch(
                    'matches',
                    queryset=Match.objects.select_related('home_team', 'away_team').order_by('sort_order', 'id'),
                )
            )
            .order_by('date', 'number')
        )

        schedule_weeks = [
            {
                'week': week,
                'entries': build_week_schedule_with_byes(active_league, week),
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


def archived_seasons(request):
    active_league = get_active_league(request)

    archived_season_options = []
    selected_archived_season = None
    archived_team_standings = []
    archived_player_standings = []

    if active_league:
        archived_seasons_qs = active_league.archived_seasons.prefetch_related(
            'teams',
            'players',
        ).order_by('-archived_at', '-id')

        archived_season_options = list(archived_seasons_qs)

        selected_archived_season_id = request.GET.get('season')
        if selected_archived_season_id:
            selected_archived_season = archived_seasons_qs.filter(
                pk=selected_archived_season_id
            ).first()

        if selected_archived_season is None:
            selected_archived_season = archived_season_options[0] if archived_season_options else None

    if selected_archived_season:
        archived_team_standings = [
            {
                'team_name': team.team_name,
                'matches_won': team.matches_won,
                'matches_lost': team.matches_lost,
                'games_won': team.games_won,
                'games_lost': team.games_lost,
            }
            for team in selected_archived_season.teams.all()
        ]
        archived_team_standings.sort(
            key=lambda row: (-row['matches_won'], -row['games_won'], row['team_name'])
        )

        archived_player_standings = [
            {
                'player_name': player.player_name,
                'team_name': player.team_name,
                'games_won': player.games_won,
                'games_lost': player.games_lost,
                'run_outs': player.run_outs,
                'eight_on_the_breaks': player.eight_on_the_breaks,
                'sweeps': player.sweeps,
            }
            for player in selected_archived_season.players.all()
        ]
        archived_player_standings.sort(
            key=lambda row: (-row['games_won'], -row['sweeps'], row['player_name'])
        )

    return render(request, 'archived_seasons.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'archived_season_options': archived_season_options,
        'selected_archived_season': selected_archived_season,
        'archived_team_standings': archived_team_standings,
        'archived_player_standings': archived_player_standings,
    })

def build_team_player_stats(active_season, team, through_week=None):
    if not active_season:
        return []

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

    player_map = {}

    for player_result in results:
        player_id = player_result.player_id

        if player_id not in player_map:
            player_map[player_id] = {
                'player_id': player_id,
                'player': player_result.player.name,
                'team': player_result.represented_team.name,
                'male': player_result.player.male,
                'wins': 0,
                'losses': 0,
                'percentage': 0.0,
                'runs': 0,
                'sweeps': 0,
                'eights': 0,
                'tie_breaker': 0,
            }

        player_map[player_id]['wins'] += player_result.wins
        player_map[player_id]['losses'] += player_result.losses
        player_map[player_id]['runs'] += player_result.runouts
        player_map[player_id]['sweeps'] += 1 if player_result.won_all_games else 0
        player_map[player_id]['eights'] += player_result.eight_on_the_breaks

    for stat in player_map.values():
        stat['tie_breaker'] = stat['sweeps'] + stat['eights'] + stat['runs']
        total = stat['wins'] + stat['losses']
        stat['percentage'] = ((stat['wins'] / total) * 100) if total > 0 else 0.0

    return sorted(
        player_map.values(),
        key=lambda stat: (
            -stat['wins'],
            -stat['tie_breaker'],
            stat['player'],
        ),
    )