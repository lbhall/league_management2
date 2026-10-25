"""Record that a different player took over a one-pocket spot mid-season.

In a one-pocket league a "team" is really a spot in the schedule, and the
player holding it can change when someone gets hurt or runs out of time.
Until now the only option was to type the new name over the old one, which
silently merged two people's records. This records who actually played each
match instead: results before the takeover date stay with the player who
left, results from that date on go to the new player, and the spot carries on
in the schedule and standings under the new name.
"""
from django.db import transaction
from django.db.models import Q

from core.models import League, Player
from results.models import MatchResult
from scheduling.models import Season


class PlayerChangeError(ValueError):
    pass


def current_player(slot):
    return MatchResult.slot_player(slot)


@transaction.atomic
def record_player_change(slot, taking_over, took_over_on, left=''):
    """Apply a takeover of ``slot`` (a one-pocket Team).

    ``taking_over`` is the new player's name. If it differs from the slot's
    name, the slot is renamed and the previous occupant leaves the roster.
    If the slot was already renamed by hand (the common case today), pass the
    departing player's name as ``left`` so their earlier matches can be
    credited to them. Returns (old_player, new_player, reassigned_count).
    """
    league = slot.league
    if league.results_type != League.ResultsType.ONE_POCKET:
        raise PlayerChangeError('Player changes are only recorded for one-pocket leagues.')

    taking_over = (taking_over or '').strip()
    left = (left or '').strip()
    if not taking_over:
        raise PlayerChangeError('Enter the name of the player taking over.')

    occupant = current_player(slot)

    if taking_over.lower() != slot.name.strip().lower():
        # The spot still shows the departing player: rename it and swap the roster.
        old = occupant
        new, _ = Player.objects.get_or_create(league=league, name=taking_over, defaults={'male': True})
        if old is not None and old.id == new.id:
            raise PlayerChangeError(f'{taking_over} already holds this spot.')
        if old is not None:
            old.team = None
            old.save(update_fields=['team'])
        new.team = slot
        new.save(update_fields=['team'])
        slot.name = taking_over
        slot.captain = new
        slot.save()
    else:
        # The spot was already renamed by hand; we only need the name of whoever left.
        if not left:
            raise PlayerChangeError(
                f'The spot is already named {slot.name}. Enter the name of the player who left '
                'so their earlier matches can be credited to them.'
            )
        if left.lower() == taking_over.lower():
            raise PlayerChangeError('The player who left and the player taking over are the same name.')
        new = occupant
        if new is None:
            new, _ = Player.objects.get_or_create(league=league, name=taking_over, defaults={'male': True})
            new.team = slot
            new.save(update_fields=['team'])
        old, _ = Player.objects.get_or_create(league=league, name=left, defaults={'male': True})
        if old.team_id == slot.id and old.id != new.id:
            old.team = None
            old.save(update_fields=['team'])

    reassigned = 0
    results = (
        MatchResult.objects.filter(match__week__season__league=league)
        .exclude(match__week__season__status=Season.Status.ARCHIVED)
        .filter(Q(match__home_team=slot) | Q(match__away_team=slot))
        .select_related('match__week')
    )
    for result in results:
        player = old if result.match.week.date < took_over_on else new
        if player is None:
            continue
        field = 'home_player' if result.match.home_team_id == slot.id else 'away_player'
        if getattr(result, f'{field}_id') != player.id:
            setattr(result, field, player)
            result.save(update_fields=[field, 'updated_at'])
            reassigned += 1

    return old, new, reassigned
