from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import League
from results.models import MatchResult, PlayerMatchResult
from scheduling.models import Match, Season

from .forms import LoginForm, SignupForm
from .models import ScoringProfile


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

    match = get_object_or_404(
        Match.objects.select_related(
            'home_team', 'away_team', 'week__season__league'
        ),
        pk=match_id,
    )

    if not profile.can_score_match(match):
        messages.error(request, 'You are not allowed to enter scores for this match.')
        return redirect('scoring:match_list')

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

    sections = []
    for team in editable_teams:
        roster = list(team.players.order_by('name'))
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
        'team_size': team_size,
        'win_range': range(team_size + 1),
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
