from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from core.models import Player, League
from scheduling.models import Match
from .models import MatchResult, PlayerMatchResult


def get_user_league(request):
    if request.user.is_superuser:
        return None

    access = getattr(request.user, 'league_admin_access', None)
    return access.league if access else None


@admin.register(MatchResult)
class MatchResultAdmin(admin.ModelAdmin):
    list_display = ('match', 'home_team_score', 'away_team_score', 'created_at', 'updated_at')
    search_fields = (
        'match__home_team__name',
        'match__away_team__name',
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        user_league = get_user_league(request)

        if request.user.is_superuser or user_league is None:
            return queryset

        return queryset.filter(match__week__season__league=user_league)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'enter-score/<int:match_id>/',
                self.admin_site.admin_view(self.enter_score_view),
                name='results_matchresult_enter_score',
            ),
        ]
        return custom_urls + urls

    def _get_scoped_match(self, request, match_id):
        queryset = Match.objects.select_related(
            'week__season__league',
            'home_team',
            'away_team',
        )

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            queryset = queryset.filter(week__season__league=user_league)

        return get_object_or_404(queryset, pk=match_id)

    def _player_choices_by_side(self, roster_players, unassigned_players):
        return {
            'team_players': roster_players,
            'unassigned_players': unassigned_players,
        }

    def _build_side_rows(self, match_result, represented_team, roster_players, unassigned_players, team_size):
        existing_results = list(
            match_result.player_results.filter(
                represented_team=represented_team,
            ).select_related('player').order_by('id')
        )

        rows = []
        for index in range(team_size):
            player_result = existing_results[index] if index < len(existing_results) else None
            player = player_result.player if player_result else None

            rows.append({
                'slot_number': index + 1,
                'player': player,
                'wins': player_result.wins if player_result else 0,
                'losses': player_result.losses if player_result else 0,
                'runouts': player_result.runouts if player_result else 0,
                'eight_on_the_breaks': player_result.eight_on_the_breaks if player_result else 0,
                'won_all_games': player_result.won_all_games if player_result else False,
                'choices': self._player_choices_by_side(roster_players, unassigned_players),
            })

        return rows

    def _calculate_team_totals(self, player_rows):
        included_rows = [row for row in player_rows if row['player']]
        return {
            'wins': sum(row['wins'] for row in included_rows),
            'losses': sum(row['losses'] for row in included_rows),
            'runouts': sum(row['runouts'] for row in included_rows),
            'eight_on_the_breaks': sum(row['eight_on_the_breaks'] for row in included_rows),
            'sweeps': sum(1 for row in included_rows if row['won_all_games']),
        }

    def _save_side_rows(self, match_result, represented_team, rows_data, valid_players, team_size):
        match_result.player_results.filter(represented_team=represented_team).delete()

        for row in rows_data:
            player_id = row.get('player_id')
            if not player_id:
                continue

            player = valid_players.get(int(player_id))
            if player is None:
                continue

            wins = int(row.get('wins', 0) or 0)
            runouts = int(row.get('runouts', 0) or 0)
            eight_on_the_breaks = int(row.get('eight_on_the_breaks', 0) or 0)

            losses = team_size - wins
            won_all_games = losses == 0

            player_result = PlayerMatchResult(
                match_result=match_result,
                player=player,
                represented_team=represented_team,
                wins=wins,
                losses=losses,
                runouts=runouts,
                eight_on_the_breaks=eight_on_the_breaks,
                won_all_games=won_all_games,
            )
            player_result.full_clean()
            player_result.save()

    def _enter_score_view_eight_ball(self, request, match, league):
        next_url = request.GET.get('next') or request.POST.get('next')

        match_result, _ = MatchResult.objects.get_or_create(match=match)

        home_players = list(
            Player.objects.filter(team=match.home_team).order_by('name')
        )
        away_players = list(
            Player.objects.filter(team=match.away_team).order_by('name')
        )
        unassigned_players = list(
            Player.objects.filter(
                league=league,
                team__isnull=True,
            ).order_by('name')
        )

        valid_players = {
            player.id: player
            for player in (home_players + away_players + unassigned_players)
        }

        team_size = league.team_size

        home_player_rows = self._build_side_rows(
            match_result=match_result,
            represented_team=match.home_team,
            roster_players=home_players,
            unassigned_players=unassigned_players,
            team_size=team_size,
        )
        away_player_rows = self._build_side_rows(
            match_result=match_result,
            represented_team=match.away_team,
            roster_players=away_players,
            unassigned_players=unassigned_players,
            team_size=team_size,
        )

        if request.method == 'POST':
            posted_home_rows = []
            posted_away_rows = []

            try:
                selected_player_ids = []
                home_total_wins = 0
                away_total_wins = 0

                for index in range(team_size):
                    home_player_id = request.POST.get(f'home_player_{index}', '').strip()
                    away_player_id = request.POST.get(f'away_player_{index}', '').strip()

                    home_wins = int(request.POST.get(f'home_wins_{index}', '0') or 0)
                    away_wins = int(request.POST.get(f'away_wins_{index}', '0') or 0)

                    posted_home_rows.append({
                        'player_id': home_player_id,
                        'wins': home_wins,
                        'runouts': request.POST.get(f'home_runouts_{index}', '0'),
                        'eight_on_the_breaks': request.POST.get(f'home_eight_on_the_breaks_{index}', '0'),
                    })
                    posted_away_rows.append({
                        'player_id': away_player_id,
                        'wins': away_wins,
                        'runouts': request.POST.get(f'away_runouts_{index}', '0'),
                        'eight_on_the_breaks': request.POST.get(f'away_eight_on_the_breaks_{index}', '0'),
                    })

                    if home_player_id:
                        selected_player_ids.append(int(home_player_id))
                        home_total_wins += home_wins
                    if away_player_id:
                        selected_player_ids.append(int(away_player_id))
                        away_total_wins += away_wins

                if len(selected_player_ids) != len(set(selected_player_ids)):
                    self.message_user(
                        request,
                        'A player cannot be selected in more than one slot.',
                        level=messages.ERROR,
                    )
                else:
                    expected_total_games = team_size * team_size
                    actual_total_games = home_total_wins + away_total_wins

                    if actual_total_games != expected_total_games:
                        self.message_user(
                            request,
                            (
                                f'The total wins entered must equal {expected_total_games} games. '
                                f'Currently entered: {actual_total_games}.'
                            ),
                            level=messages.ERROR,
                        )
                    else:
                        self._save_side_rows(
                            match_result=match_result,
                            represented_team=match.home_team,
                            rows_data=posted_home_rows,
                            valid_players=valid_players,
                            team_size=team_size,
                        )
                        self._save_side_rows(
                            match_result=match_result,
                            represented_team=match.away_team,
                            rows_data=posted_away_rows,
                            valid_players=valid_players,
                            team_size=team_size,
                        )

                        self.message_user(
                            request,
                            'Match score saved successfully.',
                            level=messages.SUCCESS,
                        )

                        if next_url:
                            return redirect(next_url)

                        return redirect(
                            reverse('admin:results_matchresult_enter_score', args=[match.id])
                        )

            except ValueError:
                self.message_user(
                    request,
                    'Please enter valid numeric values for players, wins, runouts, and 8 on the breaks.',
                    level=messages.ERROR,
                )

            for index, row in enumerate(home_player_rows):
                player_id = request.POST.get(f'home_player_{index}', '').strip()
                row['player'] = valid_players.get(int(player_id)) if player_id.isdigit() else None
                row['wins'] = int(request.POST.get(f'home_wins_{index}', '0') or 0)
                row['losses'] = team_size - row['wins']
                row['runouts'] = int(request.POST.get(f'home_runouts_{index}', '0') or 0)
                row['eight_on_the_breaks'] = int(request.POST.get(f'home_eight_on_the_breaks_{index}', '0') or 0)
                row['won_all_games'] = row['losses'] == 0

            for index, row in enumerate(away_player_rows):
                player_id = request.POST.get(f'away_player_{index}', '').strip()
                row['player'] = valid_players.get(int(player_id)) if player_id.isdigit() else None
                row['wins'] = int(request.POST.get(f'away_wins_{index}', '0') or 0)
                row['losses'] = team_size - row['wins']
                row['runouts'] = int(request.POST.get(f'away_runouts_{index}', '0') or 0)
                row['eight_on_the_breaks'] = int(request.POST.get(f'away_eight_on_the_breaks_{index}', '0') or 0)
                row['won_all_games'] = row['losses'] == 0

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'title': f'Enter Score: {match}',
            'match': match,
            'league': league,
            'team_size': team_size,
            'next_url': next_url,
            'home_player_rows': home_player_rows,
            'away_player_rows': away_player_rows,
            'home_totals': self._calculate_team_totals(home_player_rows),
            'away_totals': self._calculate_team_totals(away_player_rows),
        }

        return TemplateResponse(
            request,
            'admin/results/matchresult/enter_score_eight_ball.html',
            context,
        )

    def _enter_score_view_one_pocket(self, request, match, league):
        next_url = request.GET.get('next') or request.POST.get('next')
        match_result, _ = MatchResult.objects.get_or_create(match=match)

        home_score = match_result.home_team_score if match_result.home_team_score is not None else 0
        away_score = match_result.away_team_score if match_result.away_team_score is not None else 0

        if request.method == 'POST':
            try:
                home_score = int(request.POST.get('home_team_score', '0') or 0)
                away_score = int(request.POST.get('away_team_score', '0') or 0)
            except ValueError:
                self.message_user(
                    request,
                    'Please enter valid numeric scores.',
                    level=messages.ERROR,
                )
            else:
                if home_score < 0 or home_score > 3 or away_score < 0 or away_score > 3:
                    self.message_user(
                        request,
                        'Scores must be between 0 and 3.',
                        level=messages.ERROR,
                    )
                elif home_score != 3 and away_score != 3:
                    self.message_user(
                        request,
                        'One team must have a winning score of 3.',
                        level=messages.ERROR,
                    )
                elif home_score == 3 and away_score == 3:
                    self.message_user(
                        request,
                        'Only one team can have a score of 3.',
                        level=messages.ERROR,
                    )
                else:
                    match_result.home_team_score = home_score
                    match_result.away_team_score = away_score
                    match_result.save(update_fields=['home_team_score', 'away_team_score', 'updated_at'])

                    self.message_user(
                        request,
                        'One pocket score saved successfully.',
                        level=messages.SUCCESS,
                    )

                    if next_url:
                        return redirect(next_url)

                    return redirect(
                        reverse('admin:results_matchresult_enter_score', args=[match.id])
                    )

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'title': f'Enter Score: {match}',
            'match': match,
            'league': league,
            'team_size': 1,
            'next_url': next_url,
            'home_score': home_score,
            'away_score': away_score,
        }

        return TemplateResponse(
            request,
            'admin/results/matchresult/enter_score_one_pocket.html',
            context,
        )


    def _enter_score_view_darts(self, request, match, league):
        pass

    def enter_score_view(self, request, match_id):
        match = self._get_scoped_match(request, match_id)
        league = match.week.season.league

        match league.results_type:
            case League.ResultsType.EIGHT_BALL:
                return self._enter_score_view_eight_ball(request, match, league)
            case League.ResultsType.ONE_POCKET:
                return self._enter_score_view_one_pocket(request, match, league)
            case League.ResultsType.DARTS:
                return self._enter_score_view_darts(request, match, league)
            case _:
                self.message_user(
                    request,
                    'Custom score entry is currently only supported for 8-ball leagues.',
                    level=messages.ERROR,
                )
                return redirect('/admin/')
