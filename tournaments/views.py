import csv
import random
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.contrib import messages
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
)
from core.views import get_active_league, get_active_season

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
