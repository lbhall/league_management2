import csv
import json
import random
import urllib.error
import urllib.request
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST
from core.models import Player, League
from scheduling.models import Season
from .models import Tournament, TournamentPlayer, TournamentTeam, BracketMatch
from .bracket import (
    generate_bracket,
    set_winner,
    can_undo,
    undo_winner,
    compute_placements,
    compute_payouts,
    _sync_tournament_completion,
    PAYOUT_PERCENTAGES,
    FLAT_PAYOUTS,
)
from core.views import get_active_league, get_active_season


def _fetch_onthehill_token():
    """Exchange the configured OnTheHill credentials for an API token."""
    url = settings.ONTHEHILL_BASE_URL.rstrip('/') + '/api/token/'
    payload = {
        'username': settings.ONTHEHILL_USERNAME,
        'password': settings.ONTHEHILL_PASSWORD,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode('utf-8')
        data = json.loads(body) if body else {}
    return data.get('token')


def _post_onthehill_payout(api_token, tournament_id, place, payout_type, amount):
    url = settings.ONTHEHILL_BASE_URL.rstrip('/') + f'/api/tournaments/{tournament_id}/payouts/'
    payload = {'place': place, 'payout_type': payout_type, 'amount': amount}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Token {api_token}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode('utf-8')
        return json.loads(body) if body else {}


@staff_member_required
def tournament_players(request):
    active_league = get_active_league(request)
    if not active_league:
        messages.error(request, "No active league found.")
        return redirect('home')

    active_season = get_active_season(active_league)
    if not active_season:
        messages.error(request, "No active season found.")
        return redirect('home')

    tournament, created = Tournament.objects.get_or_create(season=active_season)

    if request.method == 'POST':
        if 'clear_all' in request.POST:
            TournamentPlayer.objects.filter(tournament=tournament).delete()
            TournamentTeam.objects.filter(tournament=tournament).delete()
            messages.success(request, "Tournament players cleared.")
            return redirect('tournament_players')
        if 'clear_teams' in request.POST:
            TournamentTeam.objects.filter(tournament=tournament).delete()
            BracketMatch.objects.filter(tournament=tournament).delete()
            messages.success(request, "Teams cleared.")
            return redirect('tournament_players')
        if 'remove_player' in request.POST:
            player_id = request.POST.get('remove_player')
            TournamentPlayer.objects.filter(tournament=tournament, player_id=player_id).delete()
            TournamentTeam.objects.filter(tournament=tournament).delete()
            BracketMatch.objects.filter(tournament=tournament).delete()
            messages.success(request, "Player removed. Teams and bracket cleared.")
            return redirect('tournament_players')
        if 'toggle_paid' in request.POST:
            player_id = request.POST.get('toggle_paid')
            tp = TournamentPlayer.objects.filter(tournament=tournament, player_id=player_id).first()
            if tp:
                tp.paid = not tp.paid
                tp.save(update_fields=['paid'])
            return redirect('tournament_players')
        if 'make_teams' in request.POST or 'regenerate_teams' in request.POST:
            TournamentTeam.objects.filter(tournament=tournament).delete()
            # Build sorted A/B lists inline to use for pairing
            season_filter = Q(match_results__match_result__match__week__season=active_season)
            sel_players = list(Player.objects.filter(
                id__in=TournamentPlayer.objects.filter(tournament=tournament).values_list('player_id', flat=True)
            ).annotate(
                total_wins=Sum('match_results__wins', filter=season_filter),
                total_losses=Sum('match_results__losses', filter=season_filter),
            ))
            def win_pct(p):
                total = (p.total_wins or 0) + (p.total_losses or 0)
                return (p.total_wins or 0) / total if total > 0 else 0
            sel_players.sort(key=win_pct, reverse=True)
            half = (len(sel_players) + 1) // 2
            a_list = sel_players[:half]
            b_list = sel_players[half:]
            random.shuffle(b_list)
            for i, (a, b) in enumerate(zip(a_list, b_list), start=1):
                TournamentTeam.objects.create(tournament=tournament, a_player=a, b_player=b, team_number=i)
            messages.success(request, "Teams generated.")
            return redirect('tournament_players')
        player_ids = request.POST.getlist('player_ids')
        if player_ids:
            teams_existed = TournamentTeam.objects.filter(tournament=tournament).exists()
            if teams_existed:
                TournamentTeam.objects.filter(tournament=tournament).delete()
                BracketMatch.objects.filter(tournament=tournament).delete()
            for p_id in player_ids:
                TournamentPlayer.objects.get_or_create(tournament=tournament, player_id=p_id)
            if teams_existed:
                messages.success(request, "Tournament players added. Existing teams and bracket were cleared.")
            else:
                messages.success(request, "Tournament players updated.")
        return redirect('tournament_players')

    eligible_players = Player.objects.filter(
        league=active_league
    ).select_related('team').annotate(
        appearances=Count('match_results', filter=Q(match_results__match_result__match__week__season=active_season))
    ).filter(appearances__gte=2).order_by('name')

    tournament_players_qs = TournamentPlayer.objects.filter(tournament=tournament)
    selected_player_ids = set(tournament_players_qs.values_list('player_id', flat=True))
    paid_player_ids = set(tournament_players_qs.filter(paid=True).values_list('player_id', flat=True))

    # Build sorted A/B player lists for selected players
    season_filter = Q(match_results__match_result__match__week__season=active_season)
    selected_players = Player.objects.filter(
        id__in=selected_player_ids
    ).select_related('team').annotate(
        total_wins=Sum('match_results__wins', filter=season_filter),
        total_losses=Sum('match_results__losses', filter=season_filter),
    )

    def win_pct(p):
        total = (p.total_wins or 0) + (p.total_losses or 0)
        return (p.total_wins or 0) / total if total > 0 else 0

    sorted_selected = sorted(selected_players, key=win_pct, reverse=True)
    half = (len(sorted_selected) + 1) // 2
    a_players = sorted_selected[:half]
    b_players = sorted_selected[half:]

    existing_teams = list(TournamentTeam.objects.filter(tournament=tournament).select_related('a_player', 'b_player'))
    can_make_teams = len(a_players) == len(b_players) and len(a_players) > 0 and not existing_teams

    default_tournament_name = f"{active_season.name} End of Season Tournament"

    return render(request, 'tournaments/tournament_players.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'players': eligible_players,
        'selected_player_ids': selected_player_ids,
        'tournament': tournament,
        'a_players': a_players,
        'b_players': b_players,
        'existing_teams': existing_teams,
        'can_make_teams': can_make_teams,
        'paid_player_ids': paid_player_ids,
        'today': timezone.localdate(),
        'default_tournament_name': default_tournament_name,
        'default_added_money': active_league.tournament_target,
    })


@staff_member_required
def export_tournament_teams(request):
    active_league = get_active_league(request)
    active_season = get_active_season(active_league) if active_league else None
    if not active_season:
        return HttpResponse("No active season.", status=404)

    tournament = Tournament.objects.filter(season=active_season).first()
    if not tournament:
        return HttpResponse("No tournament found.", status=404)

    teams = TournamentTeam.objects.filter(tournament=tournament).select_related('a_player', 'b_player').order_by('team_number')
    if not teams:
        return HttpResponse("No teams to export.", status=404)

    response = HttpResponse(content_type='text/csv')
    filename = f"tournament_teams_{active_season.name.replace(' ', '_')}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(['Team', 'A Player', 'B Player', 'Team Name'])
    for team in teams:
        writer.writerow([
            team.team_number,
            team.a_player.name,
            team.b_player.name,
            f"{team.a_player.name} / {team.b_player.name}",
        ])

    return response


@staff_member_required
def tournament_bracket(request):
    active_league = get_active_league(request)
    if not active_league:
        messages.error(request, "No active league found.")
        return redirect('home')

    active_season = get_active_season(active_league)
    if not active_season:
        messages.error(request, "No active season found.")
        return redirect('home')

    tournament = Tournament.objects.filter(season=active_season).first()
    if not tournament:
        messages.error(request, "No tournament exists yet. Create tournament teams first.")
        return redirect('tournament_players')

    if request.method == 'POST':
        if 'generate' in request.POST:
            if not TournamentTeam.objects.filter(tournament=tournament).exists():
                messages.error(request, "Generate tournament teams before building the bracket.")
                return redirect('tournament_players')
            generate_bracket(tournament)
            messages.success(request, "Bracket generated.")
            return redirect('tournament_bracket')

        if 'clear_bracket' in request.POST:
            BracketMatch.objects.filter(tournament=tournament).delete()
            if tournament.completed_at is not None:
                tournament.completed_at = None
                tournament.save(update_fields=['completed_at'])
            messages.success(request, "Bracket cleared.")
            return redirect('tournament_bracket')

        undo_id = request.POST.get('undo_match_id')
        if undo_id:
            match = get_object_or_404(BracketMatch, id=undo_id, tournament=tournament)
            try:
                undo_winner(match)
                messages.success(request, "Match result undone.")
            except ValueError as e:
                messages.error(request, str(e))
            return redirect('tournament_bracket')

        if 'mark_complete' in request.POST:
            from django.utils import timezone
            tournament.completed_at = timezone.now()
            tournament.save(update_fields=['completed_at'])
            messages.success(request, "Tournament marked complete. Now visible on the public page.")
            return redirect('tournament_bracket')

        if 'mark_in_progress' in request.POST:
            tournament.completed_at = None
            tournament.save(update_fields=['completed_at'])
            messages.success(request, "Tournament marked in progress. Hidden from the public page.")
            return redirect('tournament_bracket')

        match_id = request.POST.get('match_id')
        winner_id = request.POST.get('winner_id')
        if match_id and winner_id:
            match = get_object_or_404(BracketMatch, id=match_id, tournament=tournament)
            if match.status == BracketMatch.STATUS_COMPLETE:
                messages.error(request, "That match is already complete.")
                return redirect('tournament_bracket')
            if str(match.team1_id) == str(winner_id):
                winner_team = match.team1
            elif str(match.team2_id) == str(winner_id):
                winner_team = match.team2
            else:
                messages.error(request, "Selected winner is not in that match.")
                return redirect('tournament_bracket')
            set_winner(match, winner_team)
            return redirect('tournament_bracket')

    # Reconcile completion state on each load: catches tournaments completed
    # before the completed_at field existed, or whose match wins were applied
    # outside the normal set_winner code path.
    if BracketMatch.objects.filter(tournament=tournament).exists():
        _sync_tournament_completion(tournament)
        tournament.refresh_from_db()

    matches = list(
        BracketMatch.objects.filter(tournament=tournament)
        .select_related('team1__a_player', 'team1__b_player', 'team2__a_player', 'team2__b_player',
                        'winner__a_player', 'winner__b_player',
                        'winner_next', 'loser_next')
    )
    for m in matches:
        m.can_undo = can_undo(m)

    def _columnize(side):
        rounds = {}
        for m in matches:
            if m.bracket_side != side:
                continue
            rounds.setdefault(m.round_number, []).append(m)
        for r in rounds:
            rounds[r].sort(key=lambda x: x.position)
        ordered = [rounds[r] for r in sorted(rounds.keys())]
        # Annotate each round with merge type for connector rendering.
        annotated = []
        for i, ms in enumerate(ordered):
            if i == len(ordered) - 1:
                merge = 'merge-none'
            else:
                next_count = len(ordered[i + 1])
                if next_count == len(ms):
                    merge = 'merge-straight'
                elif next_count * 2 == len(ms):
                    merge = 'merge-halves'
                else:
                    merge = 'merge-none'
            annotated.append({'matches': ms, 'merge_class': merge, 'label_index': i + 1})
        return annotated

    winner_rounds = _columnize(BracketMatch.SIDE_WINNER)
    loser_rounds = _columnize(BracketMatch.SIDE_LOSER)
    gf = next((m for m in matches if m.bracket_side == BracketMatch.SIDE_FINAL), None)
    reset = next((m for m in matches if m.bracket_side == BracketMatch.SIDE_RESET), None)

    placements = compute_placements(tournament) if matches else []
    payout_info = compute_payouts(tournament, placements) if matches else None

    return render(request, 'tournaments/tournament_bracket.html', {
        'active_league': active_league,
        'league_name': active_league.name if active_league else 'League Name',
        'active_season': active_season,
        'tournament': tournament,
        'winner_rounds': winner_rounds,
        'loser_rounds': loser_rounds,
        'grand_final': gf,
        'reset_match': reset,
        'has_bracket': bool(matches),
        'payout_info': payout_info,
    })


def end_of_season_tournament(request, tournament_id=None):
    """Public read-only view of completed tournaments in the active league."""
    active_league = get_active_league(request)
    if not active_league:
        messages.error(request, "No active league found.")
        return redirect('home')

    completed_tournaments = Tournament.objects.filter(
        season__league=active_league,
        completed_at__isnull=False,
    ).select_related('season').order_by('-completed_at')

    if not completed_tournaments.exists():
        return render(request, 'tournaments/end_of_season_tournament.html', {
            'active_league': active_league,
            'league_name': active_league.name,
            'completed_tournaments': [],
            'selected_tournament': None,
        })

    if tournament_id:
        selected = get_object_or_404(
            Tournament, id=tournament_id, season__league=active_league,
            completed_at__isnull=False,
        )
    else:
        selected = completed_tournaments.first()

    matches = list(
        BracketMatch.objects.filter(tournament=selected)
        .select_related('team1__a_player', 'team1__b_player', 'team2__a_player', 'team2__b_player',
                        'winner__a_player', 'winner__b_player')
    )
    for m in matches:
        m.can_undo = False  # Always read-only on the public page.

    def _columnize(side):
        rounds = {}
        for m in matches:
            if m.bracket_side != side:
                continue
            rounds.setdefault(m.round_number, []).append(m)
        for r in rounds:
            rounds[r].sort(key=lambda x: x.position)
        ordered = [rounds[r] for r in sorted(rounds.keys())]
        annotated = []
        for i, ms in enumerate(ordered):
            if i == len(ordered) - 1:
                merge = 'merge-none'
            else:
                next_count = len(ordered[i + 1])
                if next_count == len(ms):
                    merge = 'merge-straight'
                elif next_count * 2 == len(ms):
                    merge = 'merge-halves'
                else:
                    merge = 'merge-none'
            annotated.append({'matches': ms, 'merge_class': merge, 'label_index': i + 1})
        return annotated

    winner_rounds = _columnize(BracketMatch.SIDE_WINNER)
    loser_rounds = _columnize(BracketMatch.SIDE_LOSER)
    gf = next((m for m in matches if m.bracket_side == BracketMatch.SIDE_FINAL), None)
    reset = next((m for m in matches if m.bracket_side == BracketMatch.SIDE_RESET), None)

    placements = compute_placements(selected)
    payout_info = compute_payouts(selected, placements)

    return render(request, 'tournaments/end_of_season_tournament.html', {
        'active_league': active_league,
        'league_name': active_league.name,
        'completed_tournaments': completed_tournaments,
        'selected_tournament': selected,
        'winner_rounds': winner_rounds,
        'loser_rounds': loser_rounds,
        'grand_final': gf,
        'reset_match': reset,
        'payout_info': payout_info,
    })


@staff_member_required
@require_POST
def create_onthehill_tournament(request):
    active_league = get_active_league(request)
    if not active_league:
        messages.error(request, "No active league found.")
        return redirect('home')

    active_season = get_active_season(active_league)
    if not active_season:
        messages.error(request, "No active season found.")
        return redirect('home')

    tournament = Tournament.objects.filter(season=active_season).first()
    teams = []
    if tournament:
        teams = list(
            TournamentTeam.objects.filter(tournament=tournament)
            .select_related('a_player', 'b_player')
            .order_by('team_number')
        )
    if not teams:
        messages.error(request, "No tournament teams to send. Generate teams first.")
        return redirect('tournament_players')

    if not settings.ONTHEHILL_USERNAME or not settings.ONTHEHILL_PASSWORD:
        messages.error(request, "OnTheHill username/password are not configured.")
        return redirect('tournament_players')

    try:
        api_token = _fetch_onthehill_token()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            error_msg = json.loads(body).get('error') or body
        except json.JSONDecodeError:
            error_msg = body or str(e)
        messages.error(request, f"Could not get an OnTheHill API token ({e.code}): {error_msg}")
        return redirect('tournament_players')
    except urllib.error.URLError as e:
        messages.error(request, f"Could not reach OnTheHill for an API token: {e.reason}")
        return redirect('tournament_players')

    if not api_token:
        messages.error(request, "OnTheHill did not return an API token.")
        return redirect('tournament_players')

    name = (request.POST.get('name') or '').strip() or f"{active_season.name} End of Season Tournament"
    payload = {
        'name': name,
        'game_type': '8ball',
        'format': 'double_elim',
        'teams': [{'name': f"{t.a_player.name} / {t.b_player.name}"} for t in teams],
    }

    for field in ('date', 'notes'):
        value = (request.POST.get(field) or '').strip()
        if value:
            payload[field] = value

    entry_fee = (request.POST.get('entry_fee') or '').strip() or '16'
    payload['entry_fee'] = entry_fee

    added_money = (request.POST.get('added_money') or '').strip()
    if added_money:
        payload['added_money'] = added_money
    else:
        payload['added_money'] = str(active_league.tournament_target)

    venue_id = (request.POST.get('venue_id') or '').strip()
    if venue_id:
        try:
            payload['venue_id'] = int(venue_id)
        except ValueError:
            messages.error(request, "Venue ID must be an integer.")
            return redirect('tournament_players')

    url = settings.ONTHEHILL_BASE_URL.rstrip('/') + '/api/tournaments/'
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Token {api_token}',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8')
            data = json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            error_msg = json.loads(body).get('error') or body
        except json.JSONDecodeError:
            error_msg = body or str(e)
        messages.error(request, f"OnTheHill rejected the request ({e.code}): {error_msg}")
        return redirect('tournament_players')
    except urllib.error.URLError as e:
        messages.error(request, f"Could not reach OnTheHill at {url}: {e.reason}")
        return redirect('tournament_players')

    tournament_url = data.get('url')
    tournament_id = data.get('id')

    payout_errors = []
    if tournament_id:
        for place, pct in PAYOUT_PERCENTAGES.items():
            try:
                _post_onthehill_payout(api_token, tournament_id, place, 'percentage', str(round(pct * 100)))
            except urllib.error.HTTPError as e:
                payout_errors.append(f"place {place}: {e.code}")
            except urllib.error.URLError as e:
                payout_errors.append(f"place {place}: {e.reason}")
        for place, amount in FLAT_PAYOUTS.items():
            try:
                _post_onthehill_payout(api_token, tournament_id, place, 'flat', str(amount))
            except urllib.error.HTTPError as e:
                payout_errors.append(f"place {place}: {e.code}")
            except urllib.error.URLError as e:
                payout_errors.append(f"place {place}: {e.reason}")
    else:
        payout_errors.append("missing tournament id in response, payouts not sent")

    if tournament_url:
        success_msg = (
            f'Tournament created on OnTheHill. '
            f'<a href="{tournament_url}" target="_blank" rel="noopener">Open it</a>.'
        )
    else:
        success_msg = "Tournament created on OnTheHill."

    if payout_errors:
        messages.warning(
            request,
            mark_safe(success_msg + " Some payouts failed to save: " + "; ".join(payout_errors)),
        )
    else:
        messages.success(request, mark_safe(success_msg + " Payouts saved."))
    return redirect('tournament_players')
