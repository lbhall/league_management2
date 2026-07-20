from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import League, Player
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season

from .forms import LoginForm, SignupForm
from .models import GameResult, LineupSlot, ScoringProfile


def _get_profile(request):
    if not request.user.is_authenticated:
        return None
    profile = ScoringProfile.objects.filter(user=request.user).select_related(
        'league', 'player__team'
    ).first()

    if profile is None and request.user.is_staff:
        # Django admin users get an approved league-admin scoring profile
        # automatically so they can score any match without a signup step.
        league = League.objects.filter(
            results_type=League.ResultsType.EIGHT_BALL
        ).order_by('id').first()
        if league:
            profile = ScoringProfile.objects.create(
                user=request.user,
                league=league,
                role=ScoringProfile.Role.ADMIN,
                is_approved=True,
            )

    return profile


def signup(request):
    if request.user.is_authenticated:
        return redirect('scoring:match_list')

    form = SignupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        email = form.cleaned_data['email']
        player = form.cleaned_data['player']

        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=form.cleaned_data['password1'],
            )
            ScoringProfile.objects.create(
                user=user,
                league=player.league,
                player=player,
                role=ScoringProfile.Role.CAPTAIN,
            )

        login(request, user)
        return redirect('scoring:pending')

    return render(request, 'scoring/signup.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('scoring:match_list')

    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data['email'].lower().strip(),
            password=form.cleaned_data['password'],
        )
        if user is not None:
            login(request, user)
            return redirect('scoring:match_list')
        form.add_error(None, 'Invalid email or password.')

    return render(request, 'scoring/login.html', {'form': form})


@login_required(login_url='scoring:login')
def logout_view(request):
    logout(request)
    return redirect('scoring:login')


@login_required(login_url='scoring:login')
def pending_approval(request):
    profile = _get_profile(request)
    if profile and profile.is_approved:
        return redirect('scoring:match_list')
    return render(request, 'scoring/pending.html', {'profile': profile})


def _cross_side_warnings(match_result, team_size):
    """Consistency checks once both sides have rows. Every game is between the
    two teams, so each side should field the same number of players and the
    combined wins should equal the total games played."""
    match = match_result.match
    home_rows = [r for r in match_result.player_results.all() if r.represented_team_id == match.home_team_id]
    away_rows = [r for r in match_result.player_results.all() if r.represented_team_id == match.away_team_id]

    if not home_rows or not away_rows:
        return []

    warnings = []
    if len(home_rows) != len(away_rows):
        warnings.append(
            f'{match.home_team.name} has {len(home_rows)} player(s) entered but '
            f'{match.away_team.name} has {len(away_rows)} — both sides should field the same count.'
        )

    home_wins = sum(r.wins for r in home_rows)
    away_wins = sum(r.wins for r in away_rows)
    total_games = len(home_rows) * team_size
    if len(home_rows) == len(away_rows) and home_wins + away_wins != total_games:
        warnings.append(
            f'Combined wins ({home_wins} + {away_wins} = {home_wins + away_wins}) do not equal '
            f'the {total_games} games played — one side\'s scores may be off.'
        )
    return warnings


def _match_fully_scored(match):
    result = getattr(match, 'result', None)
    if result is None:
        return False
    sides_with_rows = set(
        result.player_results.values_list('represented_team_id', flat=True)
    )
    return {match.home_team_id, match.away_team_id} <= sides_with_rows


@login_required(login_url='scoring:login')
def match_list(request):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    today = timezone.localdate()
    season = Season.objects.filter(
        league=profile.league, status=Season.Status.ACTIVE
    ).first()

    current_match = None
    needs_score = []
    upcoming = []

    if season:
        matches = (
            Match.objects.filter(week__season=season)
            .select_related('home_team', 'away_team', 'week', 'result')
            .order_by('week__date', 'sort_order', 'id')
        )

        if profile.role == ScoringProfile.Role.CAPTAIN:
            team = profile.team
            matches = matches.filter(Q(home_team=team) | Q(away_team=team))

        for match in matches:
            if match.week.number is None:
                continue
            scored = _match_fully_scored(match)
            if match.week.date <= today and not scored:
                needs_score.append(match)
            elif match.week.date > today:
                upcoming.append(match)

        if needs_score:
            current_match = needs_score[0]

    return render(request, 'scoring/match_list.html', {
        'profile': profile,
        'season': season,
        'current_match': current_match,
        'needs_score': needs_score,
        'upcoming': upcoming[:5],
    })


@login_required(login_url='scoring:login')
def enter_score(request, match_id):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    match = _get_scoreable_match(request, profile, match_id)
    if match is None:
        return redirect('scoring:match_list')

    # Captains score game by game (like the paper sheet); the totals grid
    # below stays available for admins doing quick entry.
    if profile.role == ScoringProfile.Role.CAPTAIN:
        return redirect('scoring:games', match.id)

    league = match.week.season.league
    team_size = league.team_size

    if profile.role == ScoringProfile.Role.ADMIN:
        editable_teams = [match.home_team, match.away_team]
        readonly_teams = []
    else:
        editable_teams = [profile.team]
        readonly_teams = [
            match.away_team if profile.team.id == match.home_team_id else match.home_team
        ]

    result = MatchResult.objects.filter(match=match).first()
    existing = {}
    if result:
        for row in result.player_results.select_related('player'):
            existing[row.player_id] = row

    # Opponent rows shown read-only so both captains can see the full match.
    readonly_sections = []
    for team in readonly_teams:
        rows = [
            {
                'player': row.player,
                'wins': row.wins,
                'runouts': row.runouts,
                'eights': row.eight_on_the_breaks,
            }
            for row in existing.values()
            if row.represented_team_id == team.id
        ]
        rows.sort(key=lambda r: r['player'].name)
        readonly_sections.append({'team': team, 'rows': rows})

    # Unassigned league players are eligible to sub for either team.
    sub_choices = list(
        Player.objects.filter(league=league, team__isnull=True).order_by('name')
    )

    sections = []
    for team in editable_teams:
        roster = list(team.players.order_by('name'))
        # Include previously saved subs (players scoring for this team who
        # aren't on its roster) so they stay visible and editable.
        roster_ids = {p.id for p in roster}
        for row in existing.values():
            if row.represented_team_id == team.id and row.player_id not in roster_ids:
                roster.append(row.player)
                roster_ids.add(row.player_id)

        rows = []
        for player in roster:
            prior = existing.get(player.id)
            rows.append({
                'player': player,
                'played': prior is not None,
                'wins': prior.wins if prior else 0,
                'runouts': prior.runouts if prior else 0,
                'eights': prior.eight_on_the_breaks if prior else 0,
            })
        sections.append({'team': team, 'rows': rows})

    if request.method == 'POST':
        errors = []
        to_save = []
        seen_player_ids = set()
        for section in sections:
            for row in section['rows']:
                player = row['player']
                played = request.POST.get(f'played_{player.id}') == 'on'
                try:
                    wins = int(request.POST.get(f'wins_{player.id}', '0') or 0)
                    runouts = int(request.POST.get(f'runouts_{player.id}', '0') or 0)
                    eights = int(request.POST.get(f'eights_{player.id}', '0') or 0)
                except ValueError:
                    errors.append(f'Invalid number for {player.name}.')
                    continue

                row.update({'played': played, 'wins': wins, 'runouts': runouts, 'eights': eights})

                if not played:
                    continue
                if wins < 0 or wins > team_size:
                    errors.append(f'{player.name}: wins must be between 0 and {team_size}.')
                if runouts < 0 or runouts > team_size:
                    errors.append(f'{player.name}: runs must be between 0 and {team_size}.')
                if eights < 0 or eights > team_size:
                    errors.append(f'{player.name}: 8-on-breaks must be between 0 and {team_size}.')
                to_save.append((section['team'], player, wins, runouts, eights))
                seen_player_ids.add(player.id)

            # Sub slots for this team.
            team = section['team']
            for slot in (1, 2):
                sub_id = request.POST.get(f'sub_player_{team.id}_{slot}', '').strip()
                if not sub_id:
                    continue
                try:
                    sub_player = Player.objects.get(
                        pk=int(sub_id), league=league, team__isnull=True,
                    )
                except (ValueError, Player.DoesNotExist):
                    errors.append('Selected sub is not an eligible unassigned player.')
                    continue
                if sub_player.id in seen_player_ids:
                    errors.append(f'{sub_player.name} is listed more than once.')
                    continue
                try:
                    wins = int(request.POST.get(f'sub_wins_{team.id}_{slot}', '0') or 0)
                    runouts = int(request.POST.get(f'sub_runouts_{team.id}_{slot}', '0') or 0)
                    eights = int(request.POST.get(f'sub_eights_{team.id}_{slot}', '0') or 0)
                except ValueError:
                    errors.append(f'Invalid number for sub {sub_player.name}.')
                    continue
                if wins < 0 or wins > team_size:
                    errors.append(f'{sub_player.name}: wins must be between 0 and {team_size}.')
                if runouts < 0 or runouts > team_size:
                    errors.append(f'{sub_player.name}: runs must be between 0 and {team_size}.')
                if eights < 0 or eights > team_size:
                    errors.append(f'{sub_player.name}: 8-on-breaks must be between 0 and {team_size}.')
                to_save.append((team, sub_player, wins, runouts, eights))
                seen_player_ids.add(sub_player.id)

        if not errors:
            with transaction.atomic():
                match_result, _ = MatchResult.objects.get_or_create(match=match)
                editable_team_ids = [t.id for t in editable_teams]
                saved_player_ids = []
                for team, player, wins, runouts, eights in to_save:
                    PlayerMatchResult.objects.update_or_create(
                        match_result=match_result,
                        player=player,
                        defaults={
                            'represented_team': team,
                            'wins': wins,
                            'losses': team_size - wins,
                            'runouts': runouts,
                            'eight_on_the_breaks': eights,
                        },
                    )
                    saved_player_ids.append(player.id)

                # Remove rows for players on the editable side(s) marked as not played.
                match_result.player_results.filter(
                    represented_team_id__in=editable_team_ids,
                ).exclude(player_id__in=saved_player_ids).delete()

            messages.success(request, 'Scores saved.')
            for warning in _cross_side_warnings(match_result, team_size):
                messages.warning(request, warning)
            return redirect('scoring:match_list')

        for error in errors:
            messages.error(request, error)

    return render(request, 'scoring/enter_score.html', {
        'profile': profile,
        'match': match,
        'sections': sections,
        'readonly_sections': readonly_sections,
        'sub_choices': sub_choices,
        'sub_slots': (1, 2),
        'team_size': team_size,
        'win_range': range(team_size + 1),
    })


def _get_scoreable_match(request, profile, match_id):
    """Fetch the match and verify this profile may score it, or return None."""
    match = get_object_or_404(
        Match.objects.select_related(
            'home_team', 'away_team', 'week__season__league'
        ),
        pk=match_id,
    )
    if not profile.can_score_match(match):
        messages.error(request, 'You are not allowed to enter scores for this match.')
        return None
    return match


def _recompute_from_games(match):
    """Once every game has a winner, roll the game results up into the
    MatchResult/PlayerMatchResult records the rest of the site reports on."""
    league = match.week.season.league
    team_size = league.team_size
    games = list(GameResult.objects.filter(match=match))
    if len(games) < team_size * team_size:
        return False

    slots = {
        (slot.team_id, slot.position): slot.player
        for slot in LineupSlot.objects.filter(match=match).select_related('player')
    }

    stats = {}

    def bump(player, team_id):
        if player.id not in stats:
            stats[player.id] = {
                'player': player, 'team_id': team_id,
                'wins': 0, 'runouts': 0, 'eights': 0,
            }
        return stats[player.id]

    for game in games:
        away_pos = GameResult.away_position_for(
            game.home_position, game.round_number, team_size
        )
        home_player = slots.get((match.home_team_id, game.home_position))
        away_player = slots.get((match.away_team_id, away_pos))
        if home_player is None or away_player is None:
            return False

        home_row = bump(home_player, match.home_team_id)
        away_row = bump(away_player, match.away_team_id)

        winner_row = home_row if game.winner == GameResult.Winner.HOME else away_row
        winner_row['wins'] += 1
        if game.runout:
            winner_row['runouts'] += 1
        if game.eight_on_break:
            winner_row['eights'] += 1

    with transaction.atomic():
        match_result, _ = MatchResult.objects.get_or_create(match=match)
        for row in stats.values():
            PlayerMatchResult.objects.update_or_create(
                match_result=match_result,
                player=row['player'],
                defaults={
                    'represented_team_id': row['team_id'],
                    'wins': row['wins'],
                    'losses': team_size - row['wins'],
                    'runouts': row['runouts'],
                    'eight_on_the_breaks': row['eights'],
                },
            )
        match_result.player_results.exclude(player_id__in=stats.keys()).delete()
    return True


@login_required(login_url='scoring:login')
def lineup(request, match_id):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    match = _get_scoreable_match(request, profile, match_id)
    if match is None:
        return redirect('scoring:match_list')

    league = match.week.season.league
    team_size = league.team_size
    positions = list(range(1, team_size + 1))

    existing = {
        (slot.team_id, slot.position): slot.player_id
        for slot in LineupSlot.objects.filter(match=match)
    }

    # Own team first for captains so their lineup is at the top.
    teams = [match.home_team, match.away_team]
    if (
        profile.role == ScoringProfile.Role.CAPTAIN
        and profile.team
        and profile.team.id == match.away_team_id
    ):
        teams.reverse()

    sub_choices = list(
        Player.objects.filter(league=league, team__isnull=True).order_by('name')
    )

    team_blocks = []
    for team in teams:
        is_home = team.id == match.home_team_id
        choices = list(team.players.order_by('name')) + sub_choices
        team_blocks.append({
            'team': team,
            'is_home': is_home,
            'choices': choices,
            'slots': [
                {
                    'position': pos,
                    # Home side is numbered 1-5, away side lettered A-E,
                    # matching the paper sheet.
                    'label': str(pos) if is_home else chr(64 + pos),
                    'selected': existing.get((team.id, pos)),
                }
                for pos in positions
            ],
        })

    if request.method == 'POST':
        errors = []
        new_slots = {}
        for block in team_blocks:
            team = block['team']
            valid_ids = {p.id for p in block['choices']}
            chosen = []
            for pos in positions:
                raw = request.POST.get(f'lineup_{team.id}_{pos}', '').strip()
                if not raw:
                    continue
                try:
                    player_id = int(raw)
                except ValueError:
                    errors.append(f'{team.name}: invalid player for position {pos}.')
                    continue
                if player_id not in valid_ids:
                    errors.append(f'{team.name}: player for position {pos} is not eligible.')
                    continue
                chosen.append((pos, player_id))

            if not chosen:
                continue  # side untouched — leave any existing lineup alone
            if len(chosen) != team_size:
                errors.append(f'{team.name}: all {team_size} positions must be filled.')
            player_ids = [player_id for _, player_id in chosen]
            if len(set(player_ids)) != len(player_ids):
                errors.append(f'{team.name}: each player can only appear once.')
            new_slots[team.id] = chosen

        if errors:
            for error in errors:
                messages.error(request, error)
        elif not new_slots:
            messages.error(request, 'Set the play order before saving.')
        else:
            with transaction.atomic():
                for team_id, chosen in new_slots.items():
                    LineupSlot.objects.filter(match=match, team_id=team_id).delete()
                    LineupSlot.objects.bulk_create([
                        LineupSlot(match=match, team_id=team_id, position=pos, player_id=player_id)
                        for pos, player_id in chosen
                    ])
            _recompute_from_games(match)
            messages.success(request, 'Lineup saved.')
            return redirect('scoring:games', match.id)

    return render(request, 'scoring/lineup.html', {
        'profile': profile,
        'match': match,
        'team_blocks': team_blocks,
        'positions': positions,
    })


@login_required(login_url='scoring:login')
def games(request, match_id):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    match = _get_scoreable_match(request, profile, match_id)
    if match is None:
        return redirect('scoring:match_list')

    league = match.week.season.league
    team_size = league.team_size

    slots = {
        (slot.team_id, slot.position): slot.player
        for slot in LineupSlot.objects.filter(match=match).select_related('player')
    }
    home_ready = all((match.home_team_id, pos) in slots for pos in range(1, team_size + 1))
    away_ready = all((match.away_team_id, pos) in slots for pos in range(1, team_size + 1))
    if not (home_ready and away_ready):
        messages.error(request, 'Both lineups must be set before entering games.')
        return redirect('scoring:lineup', match.id)

    existing = {
        (g.round_number, g.home_position): g
        for g in GameResult.objects.filter(match=match)
    }

    if request.method == 'POST':
        with transaction.atomic():
            for rnd in range(1, team_size + 1):
                for pos in range(1, team_size + 1):
                    winner = request.POST.get(f'winner_{rnd}_{pos}', '')
                    if winner not in (GameResult.Winner.HOME, GameResult.Winner.AWAY):
                        continue
                    GameResult.objects.update_or_create(
                        match=match,
                        round_number=rnd,
                        home_position=pos,
                        defaults={
                            'winner': winner,
                            'runout': request.POST.get(f'ro_{rnd}_{pos}') == 'on',
                            'eight_on_break': request.POST.get(f'eb_{rnd}_{pos}') == 'on',
                        },
                    )
        completed = _recompute_from_games(match)
        if completed:
            messages.success(request, 'All games recorded — match totals saved.')
            return redirect('scoring:match_list')
        messages.success(request, 'Games saved. Keep going!')
        return redirect('scoring:games', match.id)

    rounds = []
    games_entered = 0
    for rnd in range(1, team_size + 1):
        game_rows = []
        for pos in range(1, team_size + 1):
            away_pos = GameResult.away_position_for(pos, rnd, team_size)
            game = existing.get((rnd, pos))
            if game:
                games_entered += 1
            game_rows.append({
                'home_position': pos,
                'away_position': away_pos,
                'away_letter': chr(64 + away_pos),
                'home_player': slots[(match.home_team_id, pos)],
                'away_player': slots[(match.away_team_id, away_pos)],
                'winner': game.winner if game else '',
                'runout': game.runout if game else False,
                'eight_on_break': game.eight_on_break if game else False,
            })
        rounds.append({'number': rnd, 'games': game_rows})

    return render(request, 'scoring/games.html', {
        'profile': profile,
        'match': match,
        'rounds': rounds,
        'total_games': team_size * team_size,
        'games_entered': games_entered,
    })


@login_required(login_url='scoring:login')
def add_player(request):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    next_url = request.GET.get('next') or request.POST.get('next') or ''
    can_add_to_team = (
        profile.role == ScoringProfile.Role.CAPTAIN and profile.team is not None
    )

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        male = request.POST.get('gender', 'male') == 'male'
        assignment = request.POST.get('assignment', 'sub')

        if not name:
            messages.error(request, 'Player name is required.')
        elif Player.objects.filter(league=profile.league, name__iexact=name).exists():
            messages.error(request, f'A player named "{name}" already exists in this league.')
        else:
            team = None
            if assignment == 'team' and can_add_to_team:
                team = profile.team
            player = Player.objects.create(
                league=profile.league,
                team=team,
                name=name,
                male=male,
            )
            if team:
                messages.success(request, f'{player.name} added to {team.name}.')
            else:
                messages.success(request, f'{player.name} added as a sub (no team).')
            if next_url.startswith('/score/'):
                return redirect(next_url)
            return redirect('scoring:match_list')

    return render(request, 'scoring/add_player.html', {
        'profile': profile,
        'next_url': next_url,
        'can_add_to_team': can_add_to_team,
    })


def manifest(request):
    return JsonResponse({
        'name': 'EMC League Scoring',
        'short_name': 'EMC Score',
        'start_url': '/score/',
        'scope': '/score/',
        'display': 'standalone',
        'background_color': '#0c385f',
        'theme_color': '#0c385f',
        'icons': [
            {'src': '/static/scoring/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
            {'src': '/static/scoring/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
        ],
    })


def service_worker(request):
    js = """
self.addEventListener('install', function(event) { self.skipWaiting(); });
self.addEventListener('activate', function(event) { event.waitUntil(clients.claim()); });
self.addEventListener('fetch', function(event) {
  event.respondWith(
    fetch(event.request).catch(function() {
      return caches.match(event.request);
    })
  );
});
"""
    return HttpResponse(js, content_type='application/javascript')
