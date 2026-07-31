from django.conf import settings
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
from .models import (
    GameResult,
    LineupSlot,
    MatchEntry,
    MatchEntryGame,
    MatchEntryLineup,
    ScoringProfile,
)


SCOREABLE_TYPES = (
    League.ResultsType.EIGHT_BALL,
    League.ResultsType.ONE_POCKET,
    League.ResultsType.DARTS,
)


def _admin_allowed_leagues(user):
    """Leagues an admin profile may switch between: superusers get all,
    staff scoped via LeagueAdminAccess get only their league."""
    queryset = League.objects.filter(results_type__in=SCOREABLE_TYPES).order_by('name')
    if user.is_superuser:
        return list(queryset)
    access = getattr(user, 'league_admin_access', None)
    if access is not None:
        return [league for league in queryset if league.pk == access.league_id]
    return []


def _get_profile(request):
    if not request.user.is_authenticated:
        return None
    profile = ScoringProfile.objects.filter(user=request.user).select_related(
        'league', 'player__team'
    ).first()

    if profile is None and request.user.is_staff:
        # Django admin users get an approved league-admin scoring profile
        # automatically — superusers on the first league, scoped staff on
        # the league their LeagueAdminAccess grants. Staff without either
        # get no profile.
        allowed = _admin_allowed_leagues(request.user)
        if allowed:
            profile = ScoringProfile.objects.create(
                user=request.user,
                league=allowed[0],
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
        identifier = form.cleaned_data['email'].strip()
        password = form.cleaned_data['password']
        # Try as entered first (usernames are case-sensitive), then
        # lowercased (captain accounts store emails lowercased).
        user = authenticate(request, username=identifier, password=password)
        if user is None and identifier.lower() != identifier:
            user = authenticate(request, username=identifier.lower(), password=password)
        if user is not None:
            login(request, user)
            return redirect('scoring:match_list')
        form.add_error(None, 'Invalid email/username or password.')

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

    league = match.week.season.league
    if league.results_type == League.ResultsType.ONE_POCKET:
        # One pocket stores the final score directly; a match is only
        # complete once someone has reached the winning 3.
        return (
            result.home_team_score is not None
            and result.away_team_score is not None
            and max(result.home_team_score, result.away_team_score) >= 3
        )
    if league.results_type == League.ResultsType.DARTS:
        # Darts also stores team scores directly; games have been recorded
        # once the totals are non-zero.
        return (
            result.home_team_score is not None
            and result.away_team_score is not None
            and result.home_team_score + result.away_team_score > 0
        )

    sides_with_rows = set(
        result.player_results.values_list('represented_team_id', flat=True)
    )
    return {match.home_team_id, match.away_team_id} <= sides_with_rows


def _result_label(match):
    """Home-away score summary for a scored match."""
    result = getattr(match, 'result', None)
    if result is None:
        return ''
    league = match.week.season.league
    if league.results_type in (League.ResultsType.ONE_POCKET, League.ResultsType.DARTS):
        return f'{result.home_team_score or 0}-{result.away_team_score or 0}'
    home_wins = 0
    away_wins = 0
    for row in result.player_results.all():
        if row.represented_team_id == match.home_team_id:
            home_wins += row.wins
        elif row.represented_team_id == match.away_team_id:
            away_wins += row.wins
    return f'{home_wins}-{away_wins}'


@login_required(login_url='scoring:login')
def match_list(request):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    admin_leagues = []
    if profile.role == ScoringProfile.Role.ADMIN:
        admin_leagues = _admin_allowed_leagues(request.user)

        requested_league_id = request.GET.get('league')
        if requested_league_id:
            new_league = next(
                (lg for lg in admin_leagues if str(lg.pk) == requested_league_id), None
            )
            if new_league and new_league.pk != profile.league_id:
                profile.league = new_league
                profile.save(update_fields=['league'])

        # If a scoped admin's profile points at a league they no longer
        # administer, snap it back to one they do.
        if admin_leagues and profile.league_id not in {lg.pk for lg in admin_leagues}:
            profile.league = admin_leagues[0]
            profile.save(update_fields=['league'])

    today = timezone.localdate()
    season = Season.objects.filter(
        league=profile.league, status=Season.Status.ACTIVE
    ).first()

    current_match = None
    needs_score = []
    upcoming = []
    recent_scored = []

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
            if scored:
                recent_scored.append({
                    'match': match,
                    'result_label': _result_label(match),
                })
            elif match.week.date <= today:
                needs_score.append(match)
            else:
                upcoming.append(match)

        if needs_score:
            current_match = needs_score[0]

        if season.league.dual_entry_scoring:
            _annotate_dual_status(needs_score, profile)

        # Upcoming: the next week's full slate, not an arbitrary cap.
        if upcoming:
            next_date = upcoming[0].week.date
            upcoming = [m for m in upcoming if m.week.date == next_date]

        # Scored: most recent first. The template shows an initial batch and
        # reveals the rest in chunks via a "show more" chevron.
        recent_scored.reverse()

    # Player-vs-player match finder for one pocket admins: finds every match
    # between the two players across all seasons, whichever order is entered.
    match_search = None
    if (
        profile.role == ScoringProfile.Role.ADMIN
        and profile.league.results_type == League.ResultsType.ONE_POCKET
    ):
        try:
            p1 = int(request.GET.get('p1', ''))
            p2 = int(request.GET.get('p2', ''))
        except ValueError:
            p1 = p2 = None

        results = []
        if p1 and p2:
            found = (
                Match.objects.filter(week__season__league=profile.league)
                .filter(
                    Q(home_team_id=p1, away_team_id=p2)
                    | Q(home_team_id=p2, away_team_id=p1)
                )
                .select_related('home_team', 'away_team', 'week__season', 'result')
                .order_by('-week__date')
            )
            results = [
                {
                    'match': m,
                    'result_label': _result_label(m),
                    'season_name': m.week.season.name,
                }
                for m in found
            ]

        match_search = {
            'teams': list(
                profile.league.teams.exclude(name__iexact='BYE').order_by('name')
            ),
            'p1': p1,
            'p2': p2,
            'results': results,
            'searched': bool(p1 and p2),
        }

    return render(request, 'scoring/match_list.html', {
        'profile': profile,
        'season': season,
        'current_match': current_match,
        'needs_score': needs_score,
        'upcoming': upcoming,
        'recent_scored': recent_scored,
        'admin_leagues': admin_leagues,
        'match_search': match_search,
        'score_upcoming': settings.SCORE_UPCOMING,
    })


@login_required(login_url='scoring:login')
def enter_score(request, match_id):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    match = _get_scoreable_match(request, profile, match_id)
    if match is None:
        return redirect('scoring:match_list')

    league = match.week.season.league

    # Dual-entry: captains capture their own entries; admins referee.
    if _dual_admin(league, profile):
        return redirect('scoring:resolve', match.id)
    if _dual_capture_active(league, profile):
        return redirect('scoring:games', match.id)

    # One pocket is a single race — just the two final scores.
    if league.results_type == League.ResultsType.ONE_POCKET:
        return _enter_score_one_pocket(request, match)

    # Darts: team games won plus per-player darts stats.
    if league.results_type == League.ResultsType.DARTS:
        return _enter_score_darts(request, match)

    # Everyone defaults to the round-robin game-by-game sheet. Admins can still
    # open the quick totals grid explicitly — the "Admin view" link adds
    # ?totals, and a totals POST comes back here.
    wants_totals = profile.role == ScoringProfile.Role.ADMIN and (
        request.method == 'POST' or request.GET.get('totals')
    )
    if not wants_totals:
        return redirect('scoring:games', match.id)

    team_size = league.team_size

    # Only admins reach here, so both teams are editable.
    editable_teams = [match.home_team, match.away_team]
    readonly_teams = []

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


def _enter_score_one_pocket(request, match):
    from core.views import get_one_pocket_race_label

    result = MatchResult.objects.filter(match=match).first()
    home_score = result.home_team_score if result else None
    away_score = result.away_team_score if result else None

    if request.method == 'POST':
        error = None
        try:
            home_score = int(request.POST.get('home_score', '') or 0)
            away_score = int(request.POST.get('away_score', '') or 0)
        except ValueError:
            error = 'Scores must be numbers.'

        if error is None:
            if home_score < 0 or away_score < 0:
                error = 'Scores cannot be negative.'
            elif home_score == 3 and away_score == 3:
                error = 'Only one player can have the winning score of 3.'
            elif home_score != 3 and away_score != 3:
                error = 'One player must have the winning score of 3.'
            elif max(home_score, away_score) > 3:
                error = 'The winning score is exactly 3.'

        if error is None:
            match_result, _ = MatchResult.objects.get_or_create(match=match)
            match_result.home_team_score = home_score
            match_result.away_team_score = away_score
            match_result.save()
            messages.success(request, 'Score saved.')
            return redirect('scoring:match_list')

        messages.error(request, error)

    return render(request, 'scoring/enter_score_one_pocket.html', {
        'match': match,
        'race_label': get_one_pocket_race_label(match.home_team, match.away_team),
        'home_score': home_score,
        'away_score': away_score,
        'score_range': range(4),
    })


DARTS_STAT_FIELDS = (
    ('hat_tricks', 'HT'),
    ('three_in_a_beds', '3-Bed'),
    ('white_horses', 'WH'),
    ('three_in_the_blacks', '3-Black'),
)


def _enter_score_darts(request, match):
    league = match.week.season.league
    team_size = league.team_size

    result = MatchResult.objects.filter(match=match).first()
    existing = {}
    if result:
        for row in result.player_results.select_related('player'):
            existing[row.player_id] = row

    sub_choices = list(
        Player.objects.filter(league=league, team__isnull=True).order_by('name')
    )

    def build_side(team, prefix):
        roster = list(team.players.order_by('name')) + sub_choices
        prior_rows = [
            row for row in existing.values() if row.represented_team_id == team.id
        ]
        prior_rows.sort(key=lambda r: r.player.name)
        slots = []
        for index in range(team_size):
            prior = prior_rows[index] if index < len(prior_rows) else None
            slots.append({
                'index': index,
                'prefix': prefix,
                'selected': prior.player_id if prior else None,
                'stats': [
                    {
                        'field': field,
                        'label': label,
                        'value': getattr(prior, field) if prior else 0,
                    }
                    for field, label in DARTS_STAT_FIELDS
                ],
            })
        return {'team': team, 'prefix': prefix, 'choices': roster, 'slots': slots}

    sides = [
        build_side(match.home_team, 'home'),
        build_side(match.away_team, 'away'),
    ]

    home_score = result.home_team_score if result and result.home_team_score is not None else 0
    away_score = result.away_team_score if result and result.away_team_score is not None else 0

    if request.method == 'POST':
        errors = []
        try:
            home_score = int(request.POST.get('home_team_score', '0') or 0)
            away_score = int(request.POST.get('away_team_score', '0') or 0)
        except ValueError:
            errors.append('Games won must be numbers.')

        to_save = []  # (team, player, stats dict)
        seen_player_ids = set()
        if not errors:
            if home_score < 0 or away_score < 0:
                errors.append('Games won cannot be negative.')

            for side in sides:
                valid_ids = {p.id for p in side['choices']}
                for slot in side['slots']:
                    raw = request.POST.get(f"{side['prefix']}_player_{slot['index']}", '').strip()
                    if not raw:
                        continue
                    try:
                        player_id = int(raw)
                    except ValueError:
                        errors.append('Invalid player selection.')
                        continue
                    if player_id not in valid_ids:
                        errors.append('Selected player is not eligible for this team.')
                        continue
                    if player_id in seen_player_ids:
                        errors.append('A player cannot be selected in more than one slot.')
                        continue
                    seen_player_ids.add(player_id)

                    stats = {}
                    for field, label in DARTS_STAT_FIELDS:
                        try:
                            value = int(
                                request.POST.get(f"{side['prefix']}_{field}_{slot['index']}", '0') or 0
                            )
                        except ValueError:
                            errors.append(f'{label} must be a number.')
                            value = 0
                        if value < 0:
                            errors.append(f'{label} cannot be negative.')
                        stats[field] = value

                    player = next(p for p in side['choices'] if p.id == player_id)
                    to_save.append((side['team'], player, stats))

        if not errors:
            with transaction.atomic():
                match_result, _ = MatchResult.objects.get_or_create(match=match)
                saved_ids = []
                for team, player, stats in to_save:
                    PlayerMatchResult.objects.update_or_create(
                        match_result=match_result,
                        player=player,
                        defaults={'represented_team': team, **stats},
                    )
                    saved_ids.append(player.id)
                match_result.player_results.exclude(player_id__in=saved_ids).delete()

                match_result.home_team_score = home_score
                match_result.away_team_score = away_score
                match_result.save()

            messages.success(request, 'Match score saved.')
            return redirect('scoring:match_list')

        for error in errors:
            messages.error(request, error)

    return render(request, 'scoring/enter_score_darts.html', {
        'match': match,
        'sides': sides,
        'home_score': home_score,
        'away_score': away_score,
        'stat_fields': DARTS_STAT_FIELDS,
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
    # Captains can't reopen an old finalized match (even by direct URL) — only
    # admins can change a scored match from a past week. A match from today
    # stays editable so a captain can still fix a same-day mistake (e.g. via the
    # clear-winner flow); only admins touch prior weeks.
    if (
        profile.role == ScoringProfile.Role.CAPTAIN
        and _match_fully_scored(match)
        and match.week.date < timezone.localdate()
    ):
        messages.error(request, 'This match is already scored — ask an admin to change it.')
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


# --- Dual-entry capture (phase 2) -------------------------------------------
# When a league uses dual-entry scoring, each captain records into their own
# MatchEntry (lineups + game results) instead of the shared LineupSlot/
# GameResult. Comparison and finalization land in a later phase.

def _dual_capture_active(league, profile):
    return (
        league.dual_entry_scoring
        and profile.role == ScoringProfile.Role.CAPTAIN
        and profile.team is not None
    )


def _dual_admin(league, profile):
    """An admin on a dual-entry match referees (resolve view) rather than
    entering scores directly."""
    return league.dual_entry_scoring and profile.role == ScoringProfile.Role.ADMIN


def _annotate_dual_status(matches, profile):
    """Attach a short dual-entry status label to each (unfinalized) match for
    the match list."""
    matches = list(matches)
    if not matches:
        return
    by_match = {}
    for entry in MatchEntry.objects.filter(match__in=matches):
        by_match.setdefault(entry.match_id, {})[entry.side] = entry
    is_admin = profile.role == ScoringProfile.Role.ADMIN

    def submitted(entry):
        return entry is not None and entry.status == MatchEntry.Status.SUBMITTED

    for match in matches:
        sides = by_match.get(match.id, {})
        home = sides.get(MatchEntry.Side.HOME)
        away = sides.get(MatchEntry.Side.AWAY)
        label = ''
        if submitted(home) and submitted(away):
            # Both in and still unfinalized means they disagree.
            label = 'Conflict — resolve' if is_admin else "Scores don't match — re-check"
        elif is_admin:
            count = [submitted(home), submitted(away)].count(True)
            label = f'{count} of 2 submitted'
        else:
            mine = (
                home if profile.team and profile.team.id == match.home_team_id else away
            )
            if submitted(mine):
                label = 'Submitted — waiting for the other team'
        match.dual_status_label = label


def _get_or_create_entry(match, profile):
    side = (
        MatchEntry.Side.HOME if profile.team.id == match.home_team_id
        else MatchEntry.Side.AWAY
    )
    entry, _ = MatchEntry.objects.get_or_create(match=match, side=side)
    return entry


def _reopen_if_submitted(entry):
    """Editing after submitting returns the entry to draft."""
    if entry.status == MatchEntry.Status.SUBMITTED:
        entry.status = MatchEntry.Status.DRAFT
        entry.submitted_at = None
        entry.save(update_fields=['status', 'submitted_at', 'updated_at'])


def _entry_conflicts(match, home_entry, away_entry, both_only=False):
    """Exactly which lineup spots / games / stats differ between the two
    entries — labels only, no values (captains re-check, not copy). With
    both_only, only flag genuine disagreements where BOTH sides have recorded
    the item (used for the live "as we go" view, so a half-finished sheet
    doesn't look like a pile of conflicts)."""
    team_size = match.week.season.league.team_size
    items = []

    home_lineup = {(r.team_id, r.position): r.player_id for r in home_entry.lineup.all()}
    away_lineup = {(r.team_id, r.position): r.player_id for r in away_entry.lineup.all()}
    for team in (match.home_team, match.away_team):
        is_home = team.id == match.home_team_id
        for pos in range(1, team_size + 1):
            key = (team.id, pos)
            hv = home_lineup.get(key)
            av = away_lineup.get(key)
            if both_only and (hv is None or av is None):
                continue
            if hv != av:
                label = str(pos) if is_home else chr(64 + pos)
                items.append(f'{team.name} lineup — position {label}')

    home_games = {(g.round_number, g.home_position): g for g in home_entry.games.all()}
    away_games = {(g.round_number, g.home_position): g for g in away_entry.games.all()}
    for rnd in range(1, team_size + 1):
        for pos in range(1, team_size + 1):
            g1 = home_games.get((rnd, pos))
            g2 = away_games.get((rnd, pos))
            if both_only and (g1 is None or g2 is None):
                continue
            v1 = (g1.winner, g1.runout, g1.eight_on_break) if g1 else None
            v2 = (g2.winner, g2.runout, g2.eight_on_break) if g2 else None
            if v1 == v2:
                continue
            fields = []
            if (g1.winner if g1 else None) != (g2.winner if g2 else None):
                fields.append('winner')
            if (g1.runout if g1 else None) != (g2.runout if g2 else None):
                fields.append('run out')
            if (g1.eight_on_break if g1 else None) != (g2.eight_on_break if g2 else None):
                fields.append('8 on the break')
            detail = ', '.join(fields) if fields else 'not recorded by both'
            items.append(f'Round {rnd}, Game {pos} — {detail}')
    return items


def _finalize_from_entries(match, home_entry, away_entry):
    """The two entries agree — write the authoritative records from one of them
    and roll up the match totals."""
    with transaction.atomic():
        LineupSlot.objects.filter(match=match).delete()
        LineupSlot.objects.bulk_create([
            LineupSlot(
                match=match, team_id=r.team_id, position=r.position,
                player_id=r.player_id,
            )
            for r in home_entry.lineup.all()
        ])
        GameResult.objects.filter(match=match).delete()
        GameResult.objects.bulk_create([
            GameResult(
                match=match, round_number=g.round_number,
                home_position=g.home_position, winner=g.winner,
                runout=g.runout, eight_on_break=g.eight_on_break,
            )
            for g in home_entry.games.all()
        ])
        for entry in (home_entry, away_entry):
            entry.status = MatchEntry.Status.FINALIZED
            entry.save(update_fields=['status', 'updated_at'])
    _recompute_from_games(match)


def _reconcile(match):
    """When both sides have submitted, auto-finalize if they fully agree.
    Otherwise leave both submitted — a derived conflict the captains resolve by
    adjusting and re-submitting."""
    entries = {e.side: e for e in MatchEntry.objects.filter(match=match)}
    home = entries.get(MatchEntry.Side.HOME)
    away = entries.get(MatchEntry.Side.AWAY)
    if not (home and away):
        return
    if home.status == MatchEntry.Status.SUBMITTED and away.status == MatchEntry.Status.SUBMITTED:
        if not _entry_conflicts(match, home, away):
            _finalize_from_entries(match, home, away)


def _finalize_from_side(match, source, others):
    """Admin fallback: finalize the match from a single side's entry (a
    no-show, or the admin judging one sheet correct)."""
    with transaction.atomic():
        LineupSlot.objects.filter(match=match).delete()
        LineupSlot.objects.bulk_create([
            LineupSlot(
                match=match, team_id=r.team_id, position=r.position,
                player_id=r.player_id,
            )
            for r in source.lineup.all()
        ])
        GameResult.objects.filter(match=match).delete()
        GameResult.objects.bulk_create([
            GameResult(
                match=match, round_number=g.round_number,
                home_position=g.home_position, winner=g.winner,
                runout=g.runout, eight_on_break=g.eight_on_break,
            )
            for g in source.games.all()
        ])
        for entry in [source, *others]:
            if entry:
                entry.status = MatchEntry.Status.FINALIZED
                entry.save(update_fields=['status', 'updated_at'])
    _recompute_from_games(match)


def _resolve_and_finalize(request, match, home, away):
    """Admin picks a side for each disputed item (agreed items are kept as-is),
    then the merged result is written authoritatively."""
    team_size = match.week.season.league.team_size
    hl = {(r.team_id, r.position): r for r in home.lineup.all()}
    al = {(r.team_id, r.position): r for r in away.lineup.all()}
    hg = {(g.round_number, g.home_position): g for g in home.games.all()}
    ag = {(g.round_number, g.home_position): g for g in away.games.all()}

    lineup_rows = []
    for team in (match.home_team, match.away_team):
        for pos in range(1, team_size + 1):
            h = hl.get((team.id, pos))
            a = al.get((team.id, pos))
            if h and a and h.player_id == a.player_id:
                chosen = h
            else:
                pick = request.POST.get(f'lineup_{team.id}_{pos}')
                chosen = a if pick == 'away' else h
            if chosen:
                lineup_rows.append((team.id, pos, chosen.player_id))

    game_rows = []
    for rnd in range(1, team_size + 1):
        for pos in range(1, team_size + 1):
            h = hg.get((rnd, pos))
            a = ag.get((rnd, pos))
            agree = (
                h and a
                and (h.winner, h.runout, h.eight_on_break)
                == (a.winner, a.runout, a.eight_on_break)
            )
            if agree:
                chosen = h
            else:
                pick = request.POST.get(f'game_{rnd}_{pos}')
                chosen = a if pick == 'away' else h
            if chosen:
                game_rows.append(chosen)

    with transaction.atomic():
        LineupSlot.objects.filter(match=match).delete()
        LineupSlot.objects.bulk_create([
            LineupSlot(match=match, team_id=t, position=p, player_id=pl)
            for t, p, pl in lineup_rows
        ])
        GameResult.objects.filter(match=match).delete()
        GameResult.objects.bulk_create([
            GameResult(
                match=match, round_number=g.round_number,
                home_position=g.home_position, winner=g.winner,
                runout=g.runout, eight_on_break=g.eight_on_break,
            )
            for g in game_rows
        ])
        for entry in (home, away):
            entry.status = MatchEntry.Status.FINALIZED
            entry.save(update_fields=['status', 'updated_at'])
    _recompute_from_games(match)


def _lineup_dual(request, match, profile):
    league = match.week.season.league
    team_size = league.team_size
    positions = list(range(1, team_size + 1))
    entry = _get_or_create_entry(match, profile)

    existing = {
        (row.team_id, row.position): row.player_id for row in entry.lineup.all()
    }
    # If this captain hasn't set a lineup yet but the other captain already has,
    # pre-fill from theirs so the second captain isn't retyping the same lineup
    # (they can still adjust; saving writes to this captain's own entry).
    prefilled_from_other = False
    if not existing:
        other = MatchEntry.objects.filter(match=match).exclude(side=entry.side).first()
        other_rows = list(other.lineup.all()) if other else []
        if other_rows:
            existing = {(row.team_id, row.position): row.player_id for row in other_rows}
            prefilled_from_other = True

    teams = [match.home_team, match.away_team]
    if profile.team and profile.team.id == match.away_team_id:
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
                continue
            if len(chosen) != team_size:
                errors.append(f'{team.name}: all {team_size} positions must be filled.')
            player_ids = [pid for _, pid in chosen]
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
                    entry.lineup.filter(team_id=team_id).delete()
                    MatchEntryLineup.objects.bulk_create([
                        MatchEntryLineup(
                            entry=entry, team_id=team_id, position=pos, player_id=pid,
                        )
                        for pos, pid in chosen
                    ])
                _reopen_if_submitted(entry)
            messages.success(request, 'Lineup saved.')
            return redirect('scoring:games', match.id)

    return render(request, 'scoring/lineup.html', {
        'profile': profile,
        'match': match,
        'team_blocks': team_blocks,
        'positions': positions,
        'dual_entry': True,
        'entry_status': entry.status,
        'prefilled_from_other': prefilled_from_other,
    })


def _games_dual(request, match, profile):
    league = match.week.season.league
    team_size = league.team_size
    entry = _get_or_create_entry(match, profile)

    if entry.status == MatchEntry.Status.FINALIZED and request.method == 'POST':
        messages.info(request, 'This match is finalized and can no longer be edited.')
        return redirect('scoring:match_list')

    slots = {
        (row.team_id, row.position): row.player
        for row in entry.lineup.select_related('player')
    }
    home_ready = all((match.home_team_id, pos) in slots for pos in range(1, team_size + 1))
    away_ready = all((match.away_team_id, pos) in slots for pos in range(1, team_size + 1))
    if not (home_ready and away_ready):
        messages.error(request, 'Both lineups must be set before entering games.')
        return redirect('scoring:lineup', match.id)

    existing = {
        (g.round_number, g.home_position): g for g in entry.games.all()
    }
    allow_clear = league.allow_game_winner_clear
    show_breaks = league.show_breaks

    if request.method == 'POST':
        with transaction.atomic():
            for rnd in range(1, team_size + 1):
                for pos in range(1, team_size + 1):
                    winner = request.POST.get(f'winner_{rnd}_{pos}', '')
                    if winner not in (GameResult.Winner.HOME, GameResult.Winner.AWAY):
                        if allow_clear:
                            entry.games.filter(
                                round_number=rnd, home_position=pos,
                            ).delete()
                        continue
                    eight_on_break = request.POST.get(f'eb_{rnd}_{pos}') == 'on'
                    if show_breaks and winner != GameResult.breaker_for(rnd, pos, team_size):
                        eight_on_break = False
                    MatchEntryGame.objects.update_or_create(
                        entry=entry,
                        round_number=rnd,
                        home_position=pos,
                        defaults={
                            'winner': winner,
                            'runout': request.POST.get(f'ro_{rnd}_{pos}') == 'on',
                            'eight_on_break': eight_on_break,
                        },
                    )
            _reopen_if_submitted(entry)

        if request.POST.get('submit_entry'):
            if entry.games.count() < team_size * team_size:
                messages.error(request, 'Record every game before submitting.')
                return redirect('scoring:games', match.id)
            entry.status = MatchEntry.Status.SUBMITTED
            entry.submitted_by = request.user
            entry.submitted_at = timezone.now()
            entry.save(update_fields=[
                'status', 'submitted_by', 'submitted_at', 'updated_at',
            ])
            # Compare with the other side; auto-finalize if they agree.
            _reconcile(match)
            entry.refresh_from_db()
            other = MatchEntry.objects.filter(
                match=match,
            ).exclude(side=entry.side).first()
            if entry.status == MatchEntry.Status.FINALIZED:
                messages.success(request, 'Both teams agree — match finalized.')
            elif other and other.status == MatchEntry.Status.SUBMITTED:
                messages.warning(
                    request,
                    "Your scores don't match the other team. Re-check the "
                    'flagged games, then save and re-submit.',
                )
            else:
                messages.success(
                    request, 'Scores submitted. Waiting for the other team.',
                )
            return redirect('scoring:match_list')

        messages.success(request, 'Saved. Keep going!')
        return redirect('scoring:games', match.id)

    # Import: when this captain hasn't recorded any games yet, pre-fill the
    # display from existing scores — the other captain's entry if present,
    # otherwise any scores already recorded for the match — so they start from
    # what's known rather than a blank sheet (saving writes to their own entry).
    imported = {}
    prefilled_games = False
    if not existing:
        other0 = MatchEntry.objects.filter(match=match).exclude(side=entry.side).first()
        if other0 and other0.games.exists():
            source = other0.games.all()
        else:
            source = GameResult.objects.filter(match=match)
        imported = {(g.round_number, g.home_position): g for g in source}
        prefilled_games = bool(imported)

    rounds = []
    games_entered = 0
    home_score = 0
    away_score = 0
    for rnd in range(1, team_size + 1):
        game_rows = []
        for pos in range(1, team_size + 1):
            away_pos = GameResult.away_position_for(pos, rnd, team_size)
            game = existing.get((rnd, pos)) or imported.get((rnd, pos))
            if game:
                games_entered += 1
                if game.winner == GameResult.Winner.HOME:
                    home_score += 1
                elif game.winner == GameResult.Winner.AWAY:
                    away_score += 1
            breaker = GameResult.breaker_for(rnd, pos, team_size)
            game_rows.append({
                'home_position': pos,
                'away_position': away_pos,
                'away_letter': chr(64 + away_pos),
                'home_player': slots[(match.home_team_id, pos)],
                'away_player': slots[(match.away_team_id, away_pos)],
                'winner': game.winner if game else '',
                'runout': game.runout if game else False,
                'eight_on_break': game.eight_on_break if game else False,
                'breaker': breaker,
                'home_breaks': breaker == GameResult.Winner.HOME,
                'away_breaks': breaker == GameResult.Winner.AWAY,
            })
        rounds.append({'number': rnd, 'games': game_rows})

    other = MatchEntry.objects.filter(match=match).exclude(side=entry.side).first()
    # Live "as we go" check: which items already differ from the other team,
    # counting only games both sides have recorded — so divergence surfaces
    # while entering, not just at submit.
    live_conflicts = []
    if other:
        home_e = entry if entry.side == MatchEntry.Side.HOME else other
        away_e = entry if entry.side == MatchEntry.Side.AWAY else other
        live_conflicts = _entry_conflicts(match, home_e, away_e, both_only=True)

    if entry.status == MatchEntry.Status.FINALIZED:
        dual_state = 'finalized'
    elif entry.status == MatchEntry.Status.SUBMITTED:
        if other and other.status == MatchEntry.Status.SUBMITTED and live_conflicts:
            dual_state = 'conflict'
        else:
            dual_state = 'waiting'
    else:
        dual_state = 'draft'

    return render(request, 'scoring/games.html', {
        'profile': profile,
        'match': match,
        'rounds': rounds,
        'total_games': team_size * team_size,
        'games_entered': games_entered,
        'allow_clear_winner': allow_clear,
        'show_current_score': league.show_current_score,
        'current_home_score': home_score,
        'current_away_score': away_score,
        'show_breaks': show_breaks,
        'dual_entry': True,
        'dual_state': dual_state,
        'entry_conflicts': live_conflicts,
        'prefilled_games': prefilled_games,
    })


@login_required(login_url='scoring:login')
def lineup(request, match_id):
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    match = _get_scoreable_match(request, profile, match_id)
    if match is None:
        return redirect('scoring:match_list')

    league = match.week.season.league
    if _dual_admin(league, profile):
        return redirect('scoring:resolve', match.id)
    if _dual_capture_active(league, profile):
        return _lineup_dual(request, match, profile)

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
    if _dual_admin(league, profile):
        return redirect('scoring:resolve', match.id)
    if _dual_capture_active(league, profile):
        return _games_dual(request, match, profile)

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

    allow_clear = league.allow_game_winner_clear
    show_breaks = league.show_breaks

    if request.method == 'POST':
        with transaction.atomic():
            for rnd in range(1, team_size + 1):
                for pos in range(1, team_size + 1):
                    winner = request.POST.get(f'winner_{rnd}_{pos}', '')
                    if winner not in (GameResult.Winner.HOME, GameResult.Winner.AWAY):
                        # No winner selected. When the league allows it, a
                        # deselected winner clears any previously recorded result
                        # for that game; otherwise the game is left untouched.
                        if allow_clear:
                            GameResult.objects.filter(
                                match=match, round_number=rnd, home_position=pos,
                            ).delete()
                        continue
                    eight_on_break = request.POST.get(f'eb_{rnd}_{pos}') == 'on'
                    # Only the side that broke can win on the break, so ignore an
                    # 8-on-break flag unless the winner is the breaker.
                    if show_breaks and winner != GameResult.breaker_for(rnd, pos, team_size):
                        eight_on_break = False
                    GameResult.objects.update_or_create(
                        match=match,
                        round_number=rnd,
                        home_position=pos,
                        defaults={
                            'winner': winner,
                            'runout': request.POST.get(f'ro_{rnd}_{pos}') == 'on',
                            'eight_on_break': eight_on_break,
                        },
                    )
        completed = _recompute_from_games(match)
        if not completed and allow_clear:
            # A cleared game dropped the match below complete; discard any
            # totals that were rolled up on a previous full save so it reverts
            # to "needs a score".
            MatchResult.objects.filter(match=match).delete()
        if completed:
            messages.success(request, 'All games recorded — match totals saved.')
            return redirect('scoring:match_list')
        messages.success(request, 'Games saved. Keep going!')
        return redirect('scoring:games', match.id)

    rounds = []
    games_entered = 0
    home_score = 0
    away_score = 0
    for rnd in range(1, team_size + 1):
        game_rows = []
        for pos in range(1, team_size + 1):
            away_pos = GameResult.away_position_for(pos, rnd, team_size)
            game = existing.get((rnd, pos))
            if game:
                games_entered += 1
                if game.winner == GameResult.Winner.HOME:
                    home_score += 1
                elif game.winner == GameResult.Winner.AWAY:
                    away_score += 1
            breaker = GameResult.breaker_for(rnd, pos, team_size)
            game_rows.append({
                'home_position': pos,
                'away_position': away_pos,
                'away_letter': chr(64 + away_pos),
                'home_player': slots[(match.home_team_id, pos)],
                'away_player': slots[(match.away_team_id, away_pos)],
                'winner': game.winner if game else '',
                'runout': game.runout if game else False,
                'eight_on_break': game.eight_on_break if game else False,
                'breaker': breaker,
                'home_breaks': breaker == GameResult.Winner.HOME,
                'away_breaks': breaker == GameResult.Winner.AWAY,
            })
        rounds.append({'number': rnd, 'games': game_rows})

    return render(request, 'scoring/games.html', {
        'profile': profile,
        'match': match,
        'rounds': rounds,
        'total_games': team_size * team_size,
        'games_entered': games_entered,
        'allow_clear_winner': allow_clear,
        'show_current_score': league.show_current_score,
        'current_home_score': home_score,
        'current_away_score': away_score,
        'show_breaks': show_breaks,
    })


@login_required(login_url='scoring:login')
def resolve(request, match_id):
    """Admin referee view for a dual-entry match: watch the two captains'
    submissions, resolve a conflict item-by-item, or finalize from one side
    (no-show / judgement call)."""
    profile = _get_profile(request)
    if profile is None or not profile.is_approved:
        return redirect('scoring:pending')

    match = _get_scoreable_match(request, profile, match_id)
    if match is None:
        return redirect('scoring:match_list')

    league = match.week.season.league
    if not league.dual_entry_scoring or profile.role != ScoringProfile.Role.ADMIN:
        return redirect('scoring:match_list')

    team_size = league.team_size
    entries = {e.side: e for e in MatchEntry.objects.filter(match=match)}
    home = entries.get(MatchEntry.Side.HOME)
    away = entries.get(MatchEntry.Side.AWAY)

    def _submitted(entry):
        return entry is not None and entry.status in (
            MatchEntry.Status.SUBMITTED, MatchEntry.Status.FINALIZED,
        )

    both_submitted = _submitted(home) and _submitted(away)
    finalized = (
        home and away
        and home.status == MatchEntry.Status.FINALIZED
        and away.status == MatchEntry.Status.FINALIZED
    )
    conflicts = (
        _entry_conflicts(match, home, away)
        if both_submitted and not finalized else []
    )

    if request.method == 'POST' and not finalized:
        accept = request.POST.get('accept')
        if accept in (MatchEntry.Side.HOME, MatchEntry.Side.AWAY):
            source = home if accept == MatchEntry.Side.HOME else away
            if _submitted(source):
                others = [e for e in (home, away) if e and e is not source]
                _finalize_from_side(match, source, others)
                messages.success(request, 'Match finalized.')
                return redirect('scoring:match_list')
            messages.error(request, 'That side has not submitted.')
        elif request.POST.get('resolve') and both_submitted:
            _resolve_and_finalize(request, match, home, away)
            messages.success(request, 'Conflicts resolved — match finalized.')
            return redirect('scoring:match_list')
        return redirect('scoring:resolve', match.id)

    # Build a side-by-side view of the two entries for the conflict UI.
    def _entry_view(entry):
        if entry is None:
            return None
        lineup = {
            (r.team_id, r.position): r.player for r in entry.lineup.select_related('player')
        }
        games = {(g.round_number, g.home_position): g for g in entry.games.all()}
        return {'entry': entry, 'lineup': lineup, 'games': games}

    home_view = _entry_view(home)
    away_view = _entry_view(away)

    disputed_lineup = []
    disputed_games = []
    if conflicts:
        for team in (match.home_team, match.away_team):
            is_home = team.id == match.home_team_id
            for pos in range(1, team_size + 1):
                hp = home_view['lineup'].get((team.id, pos)) if home_view else None
                ap = away_view['lineup'].get((team.id, pos)) if away_view else None
                if (hp.id if hp else None) != (ap.id if ap else None):
                    disputed_lineup.append({
                        'team': team,
                        'position': pos,
                        'label': str(pos) if is_home else chr(64 + pos),
                        'home_player': hp,
                        'away_player': ap,
                    })
        for rnd in range(1, team_size + 1):
            for pos in range(1, team_size + 1):
                hg = home_view['games'].get((rnd, pos)) if home_view else None
                ag = away_view['games'].get((rnd, pos)) if away_view else None
                hv = (hg.winner, hg.runout, hg.eight_on_break) if hg else None
                av = (ag.winner, ag.runout, ag.eight_on_break) if ag else None
                if hv != av:
                    disputed_games.append({
                        'round': rnd,
                        'position': pos,
                        'home': hg,
                        'away': ag,
                    })

    return render(request, 'scoring/resolve.html', {
        'profile': profile,
        'match': match,
        'home': home,
        'away': away,
        'both_submitted': both_submitted,
        'finalized': finalized,
        'conflicts': conflicts,
        'disputed_lineup': disputed_lineup,
        'disputed_games': disputed_games,
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
