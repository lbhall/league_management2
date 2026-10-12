"""Read a photographed 8-ball scoresheet into a draft score.

The flow is deliberately "pre-fill, then a human confirms": the photo is sent
to Claude with both rosters and a strict JSON schema, the two grids on the
sheet are cross-checked against each other using the round-robin rotation the
app already encodes, and the result is loaded into the normal review screens.
Nothing is finalized here.

Only 8-ball leagues use this (one-pocket and darts sheets are different), and
the feature is silently unavailable when no API key is configured.
"""
import base64
import io
import json
import re
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageOps

from core.models import League, Player

from .models import (
    GameResult,
    LineupSlot,
    MatchEntry,
    MatchEntryGame,
    MatchEntryLineup,
)

# Photos are downscaled before upload: plenty for reading a sheet, and it keeps
# the per-call cost flat no matter what the phone produced.
MAX_EDGE_PX = 1600
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

# $ per million tokens (input, output). Used only for the running cost shown
# to admins; the real bill is whatever the Console reports.
PRICES_PER_MTOK = {
    'claude-opus-5-5': (Decimal('4.00'), Decimal('20.00')),
    'claude-sonnet-5-5': (Decimal('2.00'), Decimal('10.00')),
    'claude-haiku-5-5': (Decimal('0.10'), Decimal('0.50')),
    'claude-fable-5-1': (Decimal('10.00'), Decimal('50.00')),
}

UNREADABLE = '?'


class ScanError(Exception):
    """A problem the admin should see as a message (bad photo, API trouble)."""


def scan_available(league):
    """Whether the scoresheet reader is offered for this league."""
    return bool(settings.ANTHROPIC_API_KEY) and (
        league.results_type == League.ResultsType.EIGHT_BALL
    )


# --- image handling ----------------------------------------------------------

def prepare_image(raw_bytes):
    """Normalise an upload: honour EXIF rotation, convert to RGB JPEG and
    cap the longest edge. Returns (bytes, media_type)."""
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ScanError('That photo is too large. Please use a photo under 15 MB.')
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image = ImageOps.exif_transpose(image)
        image = image.convert('RGB')
    except Exception as exc:  # Pillow raises a mix of types for bad files
        raise ScanError('That file is not an image we can read.') from exc

    image.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX))
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=85, optimize=True)
    return out.getvalue(), 'image/jpeg'


# --- the model call ----------------------------------------------------------

def _player_row_schema(team_size):
    return {
        'type': 'object',
        'properties': {
            'slot': {'type': 'string'},
            'name': {'type': 'string'},
            'games': {
                'type': 'array',
                'items': {'type': 'string', 'enum': ['W', 'L', UNREADABLE]},
            },
            'wins': {'type': 'integer'},
        },
        'required': ['slot', 'name', 'games', 'wins'],
        'additionalProperties': False,
    }


def build_schema(team_size):
    row = _player_row_schema(team_size)
    names = {'type': 'array', 'items': {'type': 'string'}}
    return {
        'type': 'object',
        'properties': {
            'home_team': {'type': 'string'},
            'away_team': {'type': 'string'},
            'date': {'type': 'string'},
            'home_players': {'type': 'array', 'items': row},
            'away_players': {'type': 'array', 'items': row},
            'home_total': {'type': 'integer'},
            'away_total': {'type': 'integer'},
            'run_outs': names,
            'eight_on_breaks': names,
            'five_and_zero': names,
            'legibility': {'type': 'string', 'enum': ['good', 'fair', 'poor']},
            'notes': {'type': 'string'},
        },
        'required': [
            'home_team', 'away_team', 'date', 'home_players', 'away_players',
            'home_total', 'away_total', 'run_outs', 'eight_on_breaks',
            'five_and_zero', 'legibility', 'notes',
        ],
        'additionalProperties': False,
    }


SYSTEM_PROMPT = """You read handwritten pool-league scoresheets and transcribe them exactly.

The sheet is the "East Montgomery County Fun Pool League" 8-ball sheet. Layout:
- Top: Date, Home Team name (left) and Away Team name (right).
- Left grid: the home team's players in positions 1..N, one row each. Right grid: the away team's players in positions A..N, one row each.
- Each row has N game columns (1..5) followed by a Wins column. Each game cell is a handwritten W (win) or L (loss). A small printed or handwritten "B" in a cell marks who broke; ignore it.
- Below the grids: a Wins row with running totals, captain signatures, and lists headed "Run Outs", "Eight on the Break" and "5 and 0" containing player names.
- Bottom right: a "Game Matchups" grid with circled pairs like 1:A, 2:B. Ignore it.

Rules:
- Transcribe every game cell as "W", "L" or "?" if you genuinely cannot tell. Never guess a W or L you can't see; "?" is better than a wrong letter.
- Copy names as written on the sheet, including things like "(sub)". Do not substitute roster names.
- Report each row's Wins column as written; use -1 if blank or unreadable. Report the match totals the same way.
- The photo may be rotated or at an angle; read it in whatever orientation makes the printed headings upright.
- Set legibility to "poor" if several cells were hard to read."""


def _user_prompt(home_team, away_team, home_roster, away_roster, team_size):
    home_list = ', '.join(home_roster) or '(none on file)'
    away_list = ', '.join(away_roster) or '(none on file)'
    return (
        f'This should be the match {home_team} (home) vs {away_team} (away), '
        f'{team_size} players per side so {team_size * team_size} games.\n'
        f'Players on file for {home_team}: {home_list}.\n'
        f'Players on file for {away_team}: {away_list}.\n'
        'These lists are only to help you read handwriting; still copy the names as written. '
        f'Return home_players as exactly {team_size} rows with slot "1".."{team_size}" and '
        f'away_players as exactly {team_size} rows with slot "A".. in order, each with exactly '
        f'{team_size} game entries.'
    )


def estimate_cost(model, input_tokens, output_tokens):
    prices = PRICES_PER_MTOK.get(model)
    if not prices:
        return None
    per_in, per_out = prices
    return (per_in * input_tokens + per_out * output_tokens) / Decimal(1_000_000)


def extract_scoresheet(image_bytes, media_type, *, home_team, away_team,
                       home_roster, away_roster, team_size, model=None):
    """Send the photo to Claude and return (data, usage).

    ``data`` matches build_schema(); ``usage`` has model/input/output tokens
    and an estimated cost in dollars (or None when the model isn't priced).
    """
    import anthropic

    if not settings.ANTHROPIC_API_KEY:
        raise ScanError('Scoresheet reading is not configured on this server.')

    model = model or settings.SCORESHEET_SCAN_MODEL
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    encoded = base64.standard_b64encode(image_bytes).decode('ascii')

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': media_type, 'data': encoded},
                    },
                    {
                        'type': 'text',
                        'text': _user_prompt(home_team, away_team, home_roster, away_roster, team_size),
                    },
                ],
            }],
            output_config={
                'effort': 'high',
                'format': {'type': 'json_schema', 'schema': build_schema(team_size)},
            },
        )
    except anthropic.RateLimitError as exc:
        raise ScanError('The reader is busy or the spending cap was reached. Try again later.') from exc
    except anthropic.APIStatusError as exc:
        raise ScanError(f'The scoresheet reader returned an error ({exc.status_code}).') from exc
    except anthropic.APIConnectionError as exc:
        raise ScanError('Could not reach the scoresheet reader. Check the connection and try again.') from exc

    if response.stop_reason == 'refusal':
        raise ScanError('The scoresheet reader declined to read this photo.')
    if response.stop_reason == 'max_tokens':
        raise ScanError('The scoresheet reader ran out of room. Try a clearer photo.')

    text = next((block.text for block in response.content if block.type == 'text'), '')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScanError('The scoresheet reader returned something unreadable. Try again.') from exc

    usage = {
        'model': response.model,
        'input_tokens': response.usage.input_tokens,
        'output_tokens': response.usage.output_tokens,
    }
    usage['cost_usd'] = estimate_cost(model, usage['input_tokens'], usage['output_tokens'])
    return data, usage


# --- matching sheet names to players ----------------------------------------

_SUB_RE = re.compile(r'\(?\bsubs?\b\)?', re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r'[^a-z0-9 ]+')


def normalise_name(name):
    name = _SUB_RE.sub(' ', name or '')
    name = _NON_ALNUM_RE.sub(' ', name.lower())
    return ' '.join(name.split())


def match_player(sheet_name, candidates):
    """Pick the roster player a handwritten name refers to.

    Returns (player, quality) where quality is 'exact', 'partial' or 'none'.
    Only unambiguous matches are returned; a name shared by two candidates is
    left for the admin.
    """
    wanted = normalise_name(sheet_name)
    if not wanted:
        return None, 'none'

    exact = [p for p in candidates if normalise_name(p.name) == wanted]
    if len(exact) == 1:
        return exact[0], 'exact'
    if len(exact) > 1:
        return None, 'none'

    wanted_first = wanted.split()[0]
    partial = []
    for player in candidates:
        norm = normalise_name(player.name)
        if not norm:
            continue
        first = norm.split()[0]
        if norm.startswith(wanted) or wanted.startswith(norm) or first == wanted_first:
            partial.append(player)
    if len(partial) == 1:
        return partial[0], 'partial'
    return None, 'none'


# --- turning the transcript into a reviewable draft -------------------------

def _rows(data, key, team_size):
    rows = list(data.get(key) or [])
    rows = rows[:team_size]
    while len(rows) < team_size:
        rows.append({'slot': '', 'name': '', 'games': [], 'wins': -1})
    for row in rows:
        games = list(row.get('games') or [])[:team_size]
        while len(games) < team_size:
            games.append(UNREADABLE)
        row['games'] = [g if g in ('W', 'L') else UNREADABLE for g in games]
    return rows


def build_review(data, match):
    """Cross-check the transcript and resolve names against the rosters.

    The result is a JSON-serialisable dict the review page renders and
    apply_review() consumes.
    """
    league = match.week.season.league
    team_size = league.team_size
    subs = list(Player.objects.filter(league=league, team__isnull=True).order_by('name'))

    issues = []

    def lineup_for(team, rows, is_home):
        candidates = list(team.players.order_by('name')) + subs
        out = []
        seen = {}
        for index, row in enumerate(rows, start=1):
            player, quality = match_player(row.get('name', ''), candidates)
            label = str(index) if is_home else chr(64 + index)
            if player is not None and player.id in seen:
                issues.append(
                    f'{team.name}: "{row.get("name")}" and "{seen[player.id]}" both look like '
                    f'{player.name}; fix the lineup before loading.'
                )
                player, quality = None, 'none'
            if player is None:
                issues.append(f'{team.name} position {label}: could not match "{row.get("name")}" to a player.')
            else:
                seen[player.id] = row.get('name')
            out.append({
                'position': index,
                'label': label,
                'sheet_name': row.get('name', ''),
                'player_id': player.id if player else None,
                'player_name': player.name if player else '',
                'quality': quality,
                'sheet_wins': row.get('wins', -1),
                'marks': row['games'],
            })
        return out

    home_rows = _rows(data, 'home_players', team_size)
    away_rows = _rows(data, 'away_players', team_size)
    home_lineup = lineup_for(match.home_team, home_rows, True)
    away_lineup = lineup_for(match.away_team, away_rows, False)

    games = []
    home_counted = [0] * team_size
    away_counted = [0] * team_size
    for rnd in range(1, team_size + 1):
        for pos in range(1, team_size + 1):
            away_pos = GameResult.away_position_for(pos, rnd, team_size)
            home_mark = home_rows[pos - 1]['games'][rnd - 1]
            away_mark = away_rows[away_pos - 1]['games'][rnd - 1]

            winner = None
            conflict = False
            if home_mark == 'W' and away_mark in ('L', UNREADABLE):
                winner = GameResult.Winner.HOME
            elif home_mark == 'L' and away_mark in ('W', UNREADABLE):
                winner = GameResult.Winner.AWAY
            elif home_mark == UNREADABLE and away_mark == 'L':
                winner = GameResult.Winner.HOME
            elif home_mark == UNREADABLE and away_mark == 'W':
                winner = GameResult.Winner.AWAY
            elif home_mark == UNREADABLE and away_mark == UNREADABLE:
                issues.append(f'Round {rnd}, game {pos}: neither side\'s mark could be read.')
            else:
                conflict = True
                issues.append(
                    f'Round {rnd}, game {pos}: {match.home_team.name} #{pos} reads {home_mark} '
                    f'but {match.away_team.name} {chr(64 + away_pos)} also reads {away_mark}.'
                )

            if winner == GameResult.Winner.HOME:
                home_counted[pos - 1] += 1
            elif winner == GameResult.Winner.AWAY:
                away_counted[away_pos - 1] += 1

            games.append({
                'round': rnd,
                'home_position': pos,
                'away_position': away_pos,
                'away_label': chr(64 + away_pos),
                'home_mark': home_mark,
                'away_mark': away_mark,
                'winner': winner,
                'conflict': conflict,
                'runout': False,
                'eight_on_break': False,
            })

    # Wins columns are a second checksum on the reading.
    for row, counted in zip(home_lineup, home_counted):
        if row['sheet_wins'] >= 0 and row['sheet_wins'] != counted:
            issues.append(
                f'{match.home_team.name} #{row["label"]} ({row["sheet_name"]}): the Wins column says '
                f'{row["sheet_wins"]} but the game marks add up to {counted}.'
            )
    for row, counted in zip(away_lineup, away_counted):
        if row['sheet_wins'] >= 0 and row['sheet_wins'] != counted:
            issues.append(
                f'{match.away_team.name} {row["label"]} ({row["sheet_name"]}): the Wins column says '
                f'{row["sheet_wins"]} but the game marks add up to {counted}.'
            )

    home_total = sum(home_counted)
    away_total = sum(away_counted)
    if data.get('home_total', -1) >= 0 and data['home_total'] != home_total:
        issues.append(f'Home total on the sheet is {data["home_total"]} but the games add up to {home_total}.')
    if data.get('away_total', -1) >= 0 and data['away_total'] != away_total:
        issues.append(f'Away total on the sheet is {data["away_total"]} but the games add up to {away_total}.')

    # Run outs / 8-on-breaks / 5-and-0 are listed by name. Credit each to the
    # player's first won game so the per-player counts come out right; the
    # exact game can be adjusted on the games screen.
    def credit(names, field, label):
        for name in names or []:
            player, _quality = _find_in_lineup(name, home_lineup, away_lineup)
            if player is None:
                issues.append(f'{label}: could not match "{name}" to a player in the lineup.')
                continue
            side, position = player
            for game in games:
                mine = (
                    game['winner'] == GameResult.Winner.HOME and side == 'home' and game['home_position'] == position
                ) or (
                    game['winner'] == GameResult.Winner.AWAY and side == 'away' and game['away_position'] == position
                )
                if mine and not game[field]:
                    game[field] = True
                    break
            else:
                issues.append(f'{label}: "{name}" is listed but has no winning game to credit.')

    credit(data.get('run_outs'), 'runout', 'Run outs')
    credit(data.get('eight_on_breaks'), 'eight_on_break', 'Eight on the break')
    for name in data.get('five_and_zero') or []:
        player = _find_in_lineup(name, home_lineup, away_lineup)[0]
        if player is None:
            issues.append(f'5 and 0: could not match "{name}" to a player in the lineup.')
            continue
        side, position = player
        counted = home_counted[position - 1] if side == 'home' else away_counted[position - 1]
        if counted != team_size:
            issues.append(f'5 and 0: "{name}" is listed but only {counted} wins were read.')

    lineup_complete = all(r['player_id'] for r in home_lineup + away_lineup)
    games_complete = all(g['winner'] for g in games)

    return {
        'sheet': {
            'home_team': data.get('home_team', ''),
            'away_team': data.get('away_team', ''),
            'date': data.get('date', ''),
            'legibility': data.get('legibility', ''),
            'notes': data.get('notes', ''),
            'home_total': data.get('home_total', -1),
            'away_total': data.get('away_total', -1),
        },
        'team_size': team_size,
        'home_lineup': home_lineup,
        'away_lineup': away_lineup,
        'games': games,
        'home_total': home_total,
        'away_total': away_total,
        'issues': issues,
        'lineup_complete': lineup_complete,
        'games_complete': games_complete,
        'ready': lineup_complete and games_complete and not issues,
    }


def _find_in_lineup(name, home_lineup, away_lineup):
    """Locate a listed name (run outs etc.) in the transcribed lineups.
    Returns ((side, position), quality) or (None, 'none')."""
    wanted = normalise_name(name)
    if not wanted:
        return None, 'none'
    hits = []
    for side, lineup in (('home', home_lineup), ('away', away_lineup)):
        for row in lineup:
            names = {normalise_name(row['sheet_name']), normalise_name(row['player_name'])}
            names.discard('')
            if wanted in names:
                hits.append((side, row['position']))
    if len(hits) == 1:
        return hits[0], 'exact'
    if len(hits) > 1:
        return None, 'none'
    for side, lineup in (('home', home_lineup), ('away', away_lineup)):
        for row in lineup:
            for candidate in (row['sheet_name'], row['player_name']):
                norm = normalise_name(candidate)
                if norm and (norm.split()[0] == wanted.split()[0]):
                    hits.append((side, row['position']))
                    break
    if len(hits) == 1:
        return hits[0], 'partial'
    return None, 'none'


# --- loading the draft into the scoring flow --------------------------------

def apply_review(review, match, user, side=None):
    """Write the reviewed draft into the scoring records.

    Dual-entry leagues get a MatchEntry for ``side`` (home/away) so the
    existing referee screen handles comparison and finalization. Other
    leagues get LineupSlot/GameResult rows directly. Returns True when every
    lineup spot and game was loaded, False when gaps remain.
    """
    league = match.week.season.league
    if not review.get('lineup_complete'):
        raise ScanError('Every lineup position must be matched to a player before loading.')

    lineup_rows = [
        (match.home_team_id, r['position'], r['player_id']) for r in review['home_lineup']
    ] + [
        (match.away_team_id, r['position'], r['player_id']) for r in review['away_lineup']
    ]
    game_rows = [g for g in review['games'] if g['winner']]
    complete = len(game_rows) == league.team_size * league.team_size

    if league.dual_entry_scoring:
        side = side or MatchEntry.Side.HOME
        entry, _ = MatchEntry.objects.get_or_create(match=match, side=side)
        if entry.status == MatchEntry.Status.FINALIZED:
            raise ScanError('This match is already finalized.')
        entry.lineup.all().delete()
        MatchEntryLineup.objects.bulk_create([
            MatchEntryLineup(entry=entry, team_id=t, position=p, player_id=pl)
            for t, p, pl in lineup_rows
        ])
        entry.games.all().delete()
        MatchEntryGame.objects.bulk_create([
            MatchEntryGame(
                entry=entry, round_number=g['round'], home_position=g['home_position'],
                winner=g['winner'], runout=g['runout'], eight_on_break=g['eight_on_break'],
            )
            for g in game_rows
        ])
        entry.submitted_by = user
        if complete:
            entry.status = MatchEntry.Status.SUBMITTED
            entry.submitted_at = timezone.now()
        else:
            entry.status = MatchEntry.Status.DRAFT
            entry.submitted_at = None
        entry.save()
        return complete

    LineupSlot.objects.filter(match=match).delete()
    LineupSlot.objects.bulk_create([
        LineupSlot(match=match, team_id=t, position=p, player_id=pl)
        for t, p, pl in lineup_rows
    ])
    GameResult.objects.filter(match=match).delete()
    GameResult.objects.bulk_create([
        GameResult(
            match=match, round_number=g['round'], home_position=g['home_position'],
            winner=g['winner'], runout=g['runout'], eight_on_break=g['eight_on_break'],
        )
        for g in game_rows
    ])
    return complete
