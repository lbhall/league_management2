"""Extra blocks for the one-pocket player popup: form, weight split, margin,
past seasons, head-to-head records and the next match."""
from collections import defaultdict

from django.db.models import Q

from scheduling.models import ArchivedPlayerMatchResult, ArchivedTeam


def _pct_label(wins, losses):
    total = wins + losses
    return f'{round(wins / total * 100)}%' if total else '-'


def _record(rows):
    wins = sum(1 for r in rows if r['won'])
    return {'wins': wins, 'losses': len(rows) - wins, 'played': len(rows), 'pct_label': _pct_label(wins, len(rows) - wins)}


def form_strip(own_rows, length=5):
    """Last few results as dots, newest last, plus the current streak."""
    recent = own_rows[-length:]
    dots = [
        {'won': r['won'], 'title': f"{'Won' if r['won'] else 'Lost'} {r['result']} vs {r['opponent']} ({r['date']})"}
        for r in recent
    ]
    streak = ''
    if own_rows:
        last = own_rows[-1]['won']
        run = 0
        for r in reversed(own_rows):
            if r['won'] != last:
                break
            run += 1
        if run >= 2:
            streak = f"{'Won' if last else 'Lost'} {run} in a row"
    return {'dots': dots, 'streak': streak}


def weight_split(own_rows, my_rank):
    """Record when giving weight (opponent ranked lower), getting it, and even."""
    def category(row):
        opp = row['opponent_rank']
        mine = row.get('my_rank', my_rank)
        if mine is None or opp is None or opp == mine:
            return 'even'
        return 'giving' if opp < mine else 'getting'

    buckets = {'giving': [], 'even': [], 'getting': []}
    for row in own_rows:
        buckets[category(row)].append(row)
    return [
        {'key': 'giving', 'label': 'Giving weight', **_record(buckets['giving'])},
        {'key': 'even', 'label': 'Even', **_record(buckets['even'])},
        {'key': 'getting', 'label': 'Getting weight', **_record(buckets['getting'])},
    ]


def average_margin(own_rows):
    if not own_rows:
        return None
    total = 0
    for row in own_rows:
        mine, theirs = row['result'].split('-')
        total += int(mine) - int(theirs)
    margin = total / len(own_rows)
    return {'value': margin, 'label': f'{margin:+.1f}'}


def _archived_lines(player, league):
    """This person's archived match lines, by link or (older archives) by name."""
    return list(
        ArchivedPlayerMatchResult.objects.filter(
            Q(player=player) | Q(player__isnull=True, player_name__iexact=player.name),
            archived_match__archived_season__league=league,
        )
        .select_related('archived_match__archived_season')
        .prefetch_related('archived_match__player_results')
        .order_by('archived_match__date')
    )


def past_seasons(player, slot, lines=None):
    """One row per archived season the player appeared in."""
    league = slot.league
    lines = _archived_lines(player, league) if lines is None else lines

    by_season = defaultdict(lambda: {'wins': 0, 'losses': 0})
    seasons = {}
    for line in lines:
        season = line.archived_match.archived_season
        seasons[season.id] = season
        if line.archived_match.completed:
            by_season[season.id]['wins' if line.wins > line.losses else 'losses'] += 1

    rows = []
    for season_id, season in seasons.items():
        standing = (
            ArchivedTeam.objects.filter(archived_season=season, team_name__iexact=player.name).first()
            or ArchivedTeam.objects.filter(archived_season=season, team=slot).first()
        )
        wins = standing.matches_won if standing else by_season[season_id]['wins']
        losses = standing.matches_lost if standing else by_season[season_id]['losses']
        rows.append({
            'season': season.name,
            'start_date': season.start_date,
            'finish': standing.league_rank if standing else None,
            'rank': standing.team_rank if standing else None,
            'wins': wins,
            'losses': losses,
            'pct_label': _pct_label(wins, losses),
        })
    rows.sort(key=lambda r: (r['start_date'] is None, -(r['start_date'].toordinal() if r['start_date'] else 0)))
    return rows


def head_to_head(player, own_rows, lines):
    """All-time record against each opponent: this season plus archives.
    Keyed by lower-cased opponent name."""
    records = defaultdict(lambda: {'wins': 0, 'losses': 0})
    for row in own_rows:
        records[row['opponent'].strip().lower()]['wins' if row['won'] else 'losses'] += 1
    for line in lines:
        match = line.archived_match
        if not match.completed:
            continue
        others = [pr for pr in match.player_results.all() if pr.pk != line.pk]
        if not others:
            continue
        opponent = others[0].player_name.strip().lower()
        records[opponent]['wins' if line.wins > line.losses else 'losses'] += 1
    return {name: f"{rec['wins']}-{rec['losses']}" for name, rec in records.items() if rec['wins'] + rec['losses']}


def next_match(upcoming_rows, my_rank):
    if not upcoming_rows:
        return None
    from core.views import get_one_pocket_races

    row = upcoming_rows[0]
    races = get_one_pocket_races(my_rank, row['opponent_rank'])
    return {
        'date': row['date'],
        'opponent': row['opponent'],
        'opponent_rank': row['opponent_rank'],
        'race_label': f'{races[0]}/{races[1]}' if races else '',
        'h2h': row.get('h2h', ''),
    }


def build_player_extras(player, slot, own_rows, results_rows, upcoming_rows):
    """Everything the popup shows beyond the schedule itself."""
    lines = _archived_lines(player, slot.league) if player is not None else []
    h2h = head_to_head(player, own_rows, lines) if player is not None else {}
    for row in results_rows + upcoming_rows:
        row['h2h'] = h2h.get(row['opponent'].strip().lower(), '')

    return {
        'form': form_strip(own_rows),
        'weight_split': weight_split(own_rows, slot.team_rank),
        'avg_margin': average_margin(own_rows),
        'past_seasons': past_seasons(player, slot, lines) if player is not None else [],
        'next_match': next_match(upcoming_rows, slot.team_rank),
    }
