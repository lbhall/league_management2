import math
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import BracketMatch, TournamentTeam


def _sync_tournament_completion(tournament):
    """Set or clear `tournament.completed_at` based on current bracket state."""
    reset = BracketMatch.objects.filter(
        tournament=tournament, bracket_side=BracketMatch.SIDE_RESET
    ).first()
    gf = BracketMatch.objects.filter(
        tournament=tournament, bracket_side=BracketMatch.SIDE_FINAL
    ).first()

    is_done = bool(
        (reset and reset.status == BracketMatch.STATUS_COMPLETE)
        or (
            reset
            and reset.status == BracketMatch.STATUS_SKIPPED
            and gf
            and gf.status == BracketMatch.STATUS_COMPLETE
        )
        or (not reset and gf and gf.status == BracketMatch.STATUS_COMPLETE)
    )

    if is_done and tournament.completed_at is None:
        tournament.completed_at = timezone.now()
        tournament.save(update_fields=['completed_at'])
    elif not is_done and tournament.completed_at is not None:
        tournament.completed_at = None
        tournament.save(update_fields=['completed_at'])


def _standard_seeding(n):
    if n == 1:
        return [1]
    half = _standard_seeding(n // 2)
    out = []
    for s in half:
        out.append(s)
        out.append(n + 1 - s)
    return out


@transaction.atomic
def generate_bracket(tournament):
    BracketMatch.objects.filter(tournament=tournament).delete()
    if tournament.completed_at is not None:
        tournament.completed_at = None
        tournament.save(update_fields=['completed_at'])

    teams = list(TournamentTeam.objects.filter(tournament=tournament).order_by('team_number'))
    n = len(teams)
    if n < 2:
        return

    bracket_size = 1
    while bracket_size < n:
        bracket_size *= 2
    k = int(round(math.log2(bracket_size)))

    seed_order = _standard_seeding(bracket_size)
    padded = [teams[s - 1] if (s - 1) < n else None for s in seed_order]

    W = {}
    for r in range(1, k + 1):
        count = bracket_size // (2 ** r)
        for pos in range(count):
            W[(r, pos)] = BracketMatch.objects.create(
                tournament=tournament,
                bracket_side=BracketMatch.SIDE_WINNER,
                round_number=r,
                position=pos,
            )

    for i, team in enumerate(padded):
        m = W[(1, i // 2)]
        if i % 2 == 0:
            m.team1 = team
        else:
            m.team2 = team
    for (r, _pos), m in W.items():
        if r == 1:
            m.save()

    for r in range(1, k):
        count = bracket_size // (2 ** r)
        for pos in range(count):
            src = W[(r, pos)]
            src.winner_next = W[(r + 1, pos // 2)]
            src.winner_next_slot = (pos % 2) + 1
            src.save()

    L = {}
    total_lr = 2 * (k - 1) if k >= 2 else 0
    for lr in range(1, total_lr + 1):
        j = (lr + 1) // 2
        count = bracket_size // (2 ** (j + 1))
        for pos in range(count):
            L[(lr, pos)] = BracketMatch.objects.create(
                tournament=tournament,
                bracket_side=BracketMatch.SIDE_LOSER,
                round_number=lr,
                position=pos,
            )

    if total_lr >= 1:
        wr1_count = bracket_size // 2
        for pos in range(wr1_count):
            src = W[(1, pos)]
            src.loser_next = L[(1, pos // 2)]
            src.loser_next_slot = (pos % 2) + 1
            src.save()

    for lr in range(2, total_lr + 1):
        prev = lr - 1
        j = (lr + 1) // 2
        j_prev = (prev + 1) // 2
        prev_count = bracket_size // (2 ** (j_prev + 1))
        if lr % 2 == 1:
            for pos in range(prev_count):
                src = L[(prev, pos)]
                src.winner_next = L[(lr, pos // 2)]
                src.winner_next_slot = (pos % 2) + 1
                src.save()
        else:
            for pos in range(prev_count):
                src = L[(prev, pos)]
                src.winner_next = L[(lr, pos)]
                src.winner_next_slot = 1
                src.save()
            wr_round = j + 1
            wr_count = bracket_size // (2 ** wr_round)
            reverse_drop = (j % 2 == 1)
            for pos in range(wr_count):
                tgt_pos = (wr_count - 1 - pos) if reverse_drop else pos
                src = W[(wr_round, pos)]
                src.loser_next = L[(lr, tgt_pos)]
                src.loser_next_slot = 2
                src.save()

    gf = BracketMatch.objects.create(
        tournament=tournament,
        bracket_side=BracketMatch.SIDE_FINAL,
        round_number=1,
        position=0,
    )
    reset = BracketMatch.objects.create(
        tournament=tournament,
        bracket_side=BracketMatch.SIDE_RESET,
        round_number=1,
        position=0,
    )

    wr_final = W[(k, 0)]
    wr_final.winner_next = gf
    wr_final.winner_next_slot = 1
    wr_final.save()

    if total_lr >= 1:
        lr_final = L[(total_lr, 0)]
        lr_final.winner_next = gf
        lr_final.winner_next_slot = 2
        lr_final.save()
    else:
        wr_final.loser_next = gf
        wr_final.loser_next_slot = 2
        wr_final.save()

    gf.winner_next = reset
    gf.winner_next_slot = 1
    gf.loser_next = reset
    gf.loser_next_slot = 2
    gf.save()

    for m in BracketMatch.objects.filter(tournament=tournament):
        if m.team1_id and m.team2_id and m.winner_id is None and m.status != BracketMatch.STATUS_SKIPPED:
            m.status = BracketMatch.STATUS_READY
            m.save(update_fields=['status'])

    _auto_advance_byes(tournament)


def _propagate(match, winner_team, loser_team):
    if match.winner_next_id and match.winner_next_slot:
        target = match.winner_next
        if match.winner_next_slot == 1:
            target.team1 = winner_team
        else:
            target.team2 = winner_team
        if target.team1_id and target.team2_id and target.winner_id is None and target.status != BracketMatch.STATUS_SKIPPED:
            target.status = BracketMatch.STATUS_READY
        target.save()

    if loser_team and match.loser_next_id and match.loser_next_slot:
        target = match.loser_next
        if match.loser_next_slot == 1:
            target.team1 = loser_team
        else:
            target.team2 = loser_team
        if target.team1_id and target.team2_id and target.winner_id is None and target.status != BracketMatch.STATUS_SKIPPED:
            target.status = BracketMatch.STATUS_READY
        target.save()


def _resolve_match(match, winner_team, is_bye=False):
    match.winner = winner_team
    match.status = BracketMatch.STATUS_COMPLETE
    if is_bye:
        match.is_bye = True
    match.save()

    loser_team = None
    if match.team1_id == winner_team.id:
        loser_team = match.team2
    elif match.team2_id == winner_team.id:
        loser_team = match.team1

    if match.bracket_side == BracketMatch.SIDE_FINAL:
        # Grand Final: only run the Reset match when the loser-bracket finalist wins.
        if match.team2_id == winner_team.id:
            _propagate(match, winner_team, loser_team)
        else:
            reset = BracketMatch.objects.filter(
                tournament=match.tournament,
                bracket_side=BracketMatch.SIDE_RESET,
            ).first()
            if reset:
                reset.team1 = None
                reset.team2 = None
                reset.status = BracketMatch.STATUS_SKIPPED
                reset.save()
        return

    _propagate(match, winner_team, loser_team)


def _slot_has_pending_source(match, slot):
    # A completed bye match never produces a loser, so its loser_next pointer does
    # not represent a real pending source.
    return BracketMatch.objects.filter(tournament=match.tournament).filter(
        Q(winner_next=match, winner_next_slot=slot) |
        Q(loser_next=match, loser_next_slot=slot, is_bye=False)
    ).exclude(status=BracketMatch.STATUS_SKIPPED).exclude(id=match.id).exists()


def _auto_advance_byes(tournament):
    while True:
        changed = False
        qs = BracketMatch.objects.filter(
            tournament=tournament,
            winner__isnull=True,
        ).exclude(status=BracketMatch.STATUS_SKIPPED)
        for m in qs:
            slot1_filled = bool(m.team1_id)
            slot2_filled = bool(m.team2_id)
            slot1_pending = (not slot1_filled) and _slot_has_pending_source(m, 1)
            slot2_pending = (not slot2_filled) and _slot_has_pending_source(m, 2)

            # Both slots permanently empty → skip this match entirely.
            if not slot1_filled and not slot1_pending and not slot2_filled and not slot2_pending:
                m.status = BracketMatch.STATUS_SKIPPED
                m.winner_next = None
                m.winner_next_slot = None
                m.loser_next = None
                m.loser_next_slot = None
                m.save()
                changed = True
                continue

            # One slot filled, other permanently empty → bye (no loser to send).
            # We keep the loser_next pointer intact (filtered out in _slot_has_pending_source)
            # so that an undo can cascade through the bye correctly.
            if slot1_filled and not slot2_filled and not slot2_pending:
                _resolve_match(m, m.team1, is_bye=True)
                changed = True
                continue
            if slot2_filled and not slot1_filled and not slot1_pending:
                _resolve_match(m, m.team2, is_bye=True)
                changed = True
                continue
        if not changed:
            break


def set_winner(match, winner_team):
    with transaction.atomic():
        _resolve_match(match, winner_team)
        _auto_advance_byes(match.tournament)
        _sync_tournament_completion(match.tournament)


def can_undo(match):
    """Whether `match`'s result can be reversed right now.

    Only completed, non-bye matches can be undone. The downstream matches
    (winner_next and loser_next targets) must not be already complete via a
    human pick — bye-completed downstreams are fine because we cascade through
    them.
    """
    if match.status != BracketMatch.STATUS_COMPLETE or match.is_bye:
        return False
    for target in (match.winner_next, match.loser_next):
        if target and target.status == BracketMatch.STATUS_COMPLETE and not target.is_bye:
            return False
    return True


def _pull_team_from_slot(target, slot, team):
    """Remove `team` from `target.slotN`, cascading an undo if removing it un-resolves a bye."""
    if not target or not slot or not team:
        return

    if slot == 1 and target.team1_id == team.id:
        target.team1 = None
    elif slot == 2 and target.team2_id == team.id:
        target.team2 = None
    else:
        return  # slot didn't actually contain the expected team

    if target.is_bye and target.winner_id == team.id:
        target.save()
        _undo_match(target)
        return

    if target.status == BracketMatch.STATUS_READY and not (target.team1_id and target.team2_id):
        target.status = BracketMatch.STATUS_PENDING
    target.save()


def _undo_match(match):
    if match.status != BracketMatch.STATUS_COMPLETE or not match.winner_id:
        return

    winner_team = match.winner
    loser_team = _other_team(match, winner_team)

    if match.winner_next_id and match.winner_next_slot:
        _pull_team_from_slot(match.winner_next, match.winner_next_slot, winner_team)

    if loser_team and match.loser_next_id and match.loser_next_slot:
        _pull_team_from_slot(match.loser_next, match.loser_next_slot, loser_team)

    # Grand Final: also reset the Reset match (which may have been skipped or pre-filled).
    if match.bracket_side == BracketMatch.SIDE_FINAL:
        reset = BracketMatch.objects.filter(
            tournament=match.tournament,
            bracket_side=BracketMatch.SIDE_RESET,
        ).first()
        if reset:
            reset.team1 = None
            reset.team2 = None
            reset.winner = None
            reset.status = BracketMatch.STATUS_PENDING
            reset.is_bye = False
            reset.save()

    match.winner = None
    match.is_bye = False
    if match.team1_id and match.team2_id:
        match.status = BracketMatch.STATUS_READY
    else:
        match.status = BracketMatch.STATUS_PENDING
    match.save()


def undo_winner(match):
    if not can_undo(match):
        raise ValueError("This match's result cannot be undone (subsequent match already played).")
    with transaction.atomic():
        _undo_match(match)
        _sync_tournament_completion(match.tournament)


# Payout structure: percentages of the post-flat pool for top placements.
PAYOUT_PERCENTAGES = {
    1: 0.40,
    2: 0.27,
    3: 0.15,
    4: 0.08,
    5: 0.05,
    6: 0.05,
}
FLAT_PAYOUTS = {
    7: 20,
    8: 20,
}
LEAGUE_TOURNAMENT_CONTRIBUTION = 300
PER_PLAYER_TOURNAMENT_CONTRIBUTION = 8


def _other_team(match, team):
    if not team:
        return None
    if match.team1_id == team.id:
        return match.team2
    if match.team2_id == team.id:
        return match.team1
    return None


def compute_placements(tournament):
    """Return a list of (place, team) tuples ordered from 1st downward.

    Placements are derived from completed BracketMatch results. Teams still alive
    are not yet placed. Byes (no real loser) do not produce an elimination.
    """
    matches = list(BracketMatch.objects.filter(tournament=tournament)
                   .select_related('team1__a_player', 'team1__b_player',
                                   'team2__a_player', 'team2__b_player',
                                   'winner__a_player', 'winner__b_player'))
    if not matches:
        return []

    reset = next((m for m in matches if m.bracket_side == BracketMatch.SIDE_RESET), None)
    gf = next((m for m in matches if m.bracket_side == BracketMatch.SIDE_FINAL), None)

    placements = []
    final_match = None
    if reset and reset.status == BracketMatch.STATUS_COMPLETE and reset.winner_id:
        final_match = reset
    elif gf and gf.status == BracketMatch.STATUS_COMPLETE and gf.winner_id:
        # If the GF goes to a Reset, the Reset is the real final. If Reset is
        # skipped or pending, the GF is the final.
        if not reset or reset.status == BracketMatch.STATUS_SKIPPED:
            final_match = gf

    if final_match:
        placements.append((1, final_match.winner))
        loser = _other_team(final_match, final_match.winner)
        if loser:
            placements.append((2, loser))

    # 3rd onwards: walk loser bracket from final round downward.
    lr_by_round = {}
    for m in matches:
        if m.bracket_side == BracketMatch.SIDE_LOSER:
            lr_by_round.setdefault(m.round_number, []).append(m)

    place = 3
    for round_num in sorted(lr_by_round.keys(), reverse=True):
        round_losers = []
        for m in lr_by_round[round_num]:
            if m.status == BracketMatch.STATUS_COMPLETE and not m.is_bye and m.winner_id:
                loser = _other_team(m, m.winner)
                if loser:
                    round_losers.append(loser)
        if not round_losers:
            place += len(lr_by_round[round_num])  # advance counter past empty/bye round
            continue
        for loser in round_losers:
            placements.append((place, loser))
        place += len(round_losers)

    return placements


def compute_payouts(tournament, placements):
    """Return a list of (place, team, payout_dollars) given placements.

    Pool = $300 + $8 * (number of playing players, i.e. 2 × team count).
    Flat $20 each is taken off the top for 7th and 8th if those positions exist.
    Remainder distributed by PAYOUT_PERCENTAGES.
    """
    team_count = TournamentTeam.objects.filter(tournament=tournament).count()
    player_count = team_count * 2
    pool = LEAGUE_TOURNAMENT_CONTRIBUTION + PER_PLAYER_TOURNAMENT_CONTRIBUTION * player_count

    # Subtract flat payouts: every team finishing at a flat-payout place earns $20.
    flat_total = sum(FLAT_PAYOUTS[p] for p, _ in placements if p in FLAT_PAYOUTS)
    pool_after_flats = max(0, pool - flat_total)

    rows = []
    for place, team in placements:
        if place in FLAT_PAYOUTS:
            payout = FLAT_PAYOUTS[place]
        else:
            pct = PAYOUT_PERCENTAGES.get(place, 0)
            payout = pool_after_flats * pct
        rows.append((place, team, payout))
    return {
        'rows': rows,
        'pool': pool,
        'player_count': player_count,
        'player_buyin_total': PER_PLAYER_TOURNAMENT_CONTRIBUTION * player_count,
        'league_contribution': LEAGUE_TOURNAMENT_CONTRIBUTION,
        'flat_total': flat_total,
        'pool_after_flats': pool_after_flats,
    }
