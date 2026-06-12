import csv
import random
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.contrib import messages
from core.models import Player, League
from scheduling.models import Season
from .models import Tournament, TournamentPlayer, TournamentTeam
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
            messages.success(request, "Teams cleared.")
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
        for p_id in player_ids:
            TournamentPlayer.objects.get_or_create(tournament=tournament, player_id=p_id)
        messages.success(request, "Tournament players updated.")
        return redirect('tournament_players')

    eligible_players = Player.objects.filter(
        league=active_league
    ).select_related('team').annotate(
        appearances=Count('match_results', filter=Q(match_results__match_result__match__week__season=active_season))
    ).filter(appearances__gte=2).order_by('name')

    selected_player_ids = set(TournamentPlayer.objects.filter(tournament=tournament).values_list('player_id', flat=True))

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
