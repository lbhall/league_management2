"""Rank review for one-pocket leagues: which players look ready to move up
a rank, and which may belong a rank lower.

This first version works from what is recorded today: games won and lost per
match and both players' ranks. When ball counts per game are captured, the
same page gains spot-adjusted margins and the promotion/relegation stress
tests; the flags below are deliberately simple and their thresholds are
shown on the page so they can be tuned by eye.
"""
from results.models import MatchResult
from scheduling.models import Match, Season

# Flag rules. A player needs a reasonable sample before anything is said.
# The bands are deliberately wide while only game counts are recorded;
# ball counts per game (planned) will let them tighten.
MIN_GAMES = 12
UP_GAME_PCT = 0.75     # winning this share of games or more
DOWN_GAME_PCT = 0.25   # winning this share of games or less
MAX_RANK = 5
MIN_RANK = 1


def _blank_split():
    return {'matches_won': 0, 'matches_lost': 0, 'games_won': 0, 'games_lost': 0}


def _pct(won, lost):
    total = won + lost
    return (won / total) if total else None


def _pct_label(value):
    return f'{round(value * 100)}%' if value is not None else '-'


def weight_category(my_rank, opp_rank):
    """'giving' when the higher-ranked player spots the opponent, 'getting'
    when they receive the spot, 'even' at equal rank (or unknown ranks)."""
    if my_rank is None or opp_rank is None or my_rank == opp_rank:
        return 'even'
    return 'giving' if my_rank > opp_rank else 'getting'


def season_choices(league):
    """Seasons worth reviewing, newest first: the live one, then archived
    seasons that still hold their matches (soft archive)."""
    seasons = list(Season.objects.filter(league=league).prefetch_related('weeks'))
    order = {Season.Status.ACTIVE: 0, Season.Status.WORKING: 1, Season.Status.ARCHIVED: 2}

    def last_date_ordinal(season):
        dates = [w.date for w in season.weeks.all()]
        return max(dates).toordinal() if dates else 0

    # Live season first, then working, then archived; newest first within each.
    seasons.sort(key=lambda s: (order.get(s.status, 3), -last_date_ordinal(s), -s.pk))
    return seasons


def build_rank_review(league, season):
    from core.views import ONE_POCKET_COMPLETE_SCORE

    def blank_row(player, rank, active):
        return {
            'player_id': player.id,
            'name': player.name,
            'rank': rank,
            'active': active,
            'matches_won': 0,
            'matches_lost': 0,
            'games_won': 0,
            'games_lost': 0,
            'margin_total': 0,
            'splits': {'giving': _blank_split(), 'even': _blank_split(), 'getting': _blank_split()},
        }

    # One row per person: everyone currently holding a spot, plus anyone who
    # left mid-season but played matches (credited through the result's
    # home_player/away_player).
    rows = {}
    for slot in league.teams.exclude(name__iexact='BYE').order_by('name'):
        player = MatchResult.slot_player(slot)
        if player is not None:
            rows[player.id] = blank_row(player, slot.team_rank, True)

    matches = (
        Match.objects.filter(week__season=season)
        .select_related('home_team', 'away_team', 'result')
        .order_by('week__date')
    )
    for match in matches:
        result = getattr(match, 'result', None)
        if result is None:
            continue
        home_score = result.home_team_score or 0
        away_score = result.away_team_score or 0
        if max(home_score, away_score) < ONE_POCKET_COMPLETE_SCORE:
            continue

        for me, opp, mine, theirs in (
            (match.home_team, match.away_team, home_score, away_score),
            (match.away_team, match.home_team, away_score, home_score),
        ):
            if me.name.strip().upper() == 'BYE':
                continue
            player = result.side_player(me)
            if player is None:
                continue
            row = rows.get(player.id)
            if row is None:
                # Played for this spot earlier in the season, then left.
                row = rows[player.id] = blank_row(player, me.team_rank, player.team_id is not None)
            split = row['splits'][weight_category(me.team_rank, opp.team_rank)]
            won = mine > theirs
            for target in (row, split):
                target['matches_won' if won else 'matches_lost'] += 1
                target['games_won'] += mine
                target['games_lost'] += theirs
            row['margin_total'] += mine - theirs

    out = []
    for row in rows.values():
        games = row['games_won'] + row['games_lost']
        matches_played = row['matches_won'] + row['matches_lost']
        row['games_played'] = games
        row['matches_played'] = matches_played
        row['game_pct'] = _pct(row['games_won'], row['games_lost'])
        row['avg_margin'] = (row['margin_total'] / matches_played) if matches_played else None
        row['game_pct_label'] = _pct_label(row['game_pct'])
        for split in row['splits'].values():
            split['games_played'] = split['games_won'] + split['games_lost']
            split['game_pct'] = _pct(split['games_won'], split['games_lost'])
            split['game_pct_label'] = _pct_label(split['game_pct'])
        row['split_cells'] = [row['splits'][key] for key in ('giving', 'even', 'getting')]
        row['signal'], row['reason'] = _signal(row)
        out.append(row)

    out.sort(key=lambda r: (
        -(r['rank'] or 0),
        0 if r['active'] else 1,
        -(r['game_pct'] if r['game_pct'] is not None else -1),
        r['name'],
    ))
    return out


def _signal(row):
    """Return (state, reason) with state one of 'up', 'hold', 'down'."""
    rank = row['rank']
    pct = row['game_pct']
    if row['games_played'] < MIN_GAMES:
        return 'hold', f'Not enough games yet ({row["games_played"]} of {MIN_GAMES})'
    if rank is None or pct is None:
        return 'hold', 'No rank set'

    giving = row['splits']['giving']
    getting = row['splits']['getting']

    if rank < MAX_RANK and pct >= UP_GAME_PCT:
        if giving['games_played'] == 0 or giving['game_pct'] >= 0.5:
            return 'up', (
                f'Winning {pct:.0%} of games'
                + (f' and {giving["game_pct"]:.0%} when giving weight' if giving['games_played'] else '')
            )
        return 'hold', f'Winning {pct:.0%} overall but only {giving["game_pct"]:.0%} when giving weight'

    if rank > MIN_RANK and pct <= DOWN_GAME_PCT:
        if getting['games_played'] == 0 or getting['game_pct'] <= 0.5:
            return 'down', (
                f'Winning only {pct:.0%} of games'
                + (f', {getting["game_pct"]:.0%} even when getting weight' if getting['games_played'] else '')
            )
        return 'hold', f'Winning only {pct:.0%} overall but {getting["game_pct"]:.0%} when getting weight'

    if rank >= MAX_RANK and pct >= UP_GAME_PCT:
        return 'hold', f'Winning {pct:.0%} of games at the top rank'
    if rank <= MIN_RANK and pct <= DOWN_GAME_PCT:
        return 'hold', f'Winning {pct:.0%} of games at the bottom rank'
    return 'hold', f'Winning {pct:.0%} of games'


def player_outlook(league, season, player_id):
    """The rank-review verdict for one player: {'state', 'reason'} or None."""
    if season is None:
        return None
    for row in build_rank_review(league, season):
        if row['player_id'] == player_id:
            return {'state': row['signal'], 'reason': row['reason']}
    return None
