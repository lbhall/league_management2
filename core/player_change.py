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
from django.utils import timezone

from core.models import League, Player
from results.models import MatchResult
from scheduling.models import Season


class PlayerChangeError(ValueError):
    pass


def current_player(slot):
    return MatchResult.slot_player(slot)


def _find_or_create_player(league, name):
    """Like get_or_create by name, but tolerant of duplicates (oldest wins)."""
    player = Player.objects.filter(league=league, name__iexact=name).order_by('id').first()
    if player is None:
        player = Player.objects.create(league=league, name=name, male=True)
    return player


def _is_unused(player):
    """A player record nothing refers to: safe to delete as a stray duplicate."""
    from scoring.models import ScoringProfile

    if ScoringProfile.objects.filter(player=player).exists():
        return False
    return (
        player.team_id is None
        and not player.captained_teams.exists()
        and not player.home_results.exists()
        and not player.away_results.exists()
        and not player.match_results.exists()
        and not player.archived_stats.exists()
        and not player.archived_match_results.exists()
        and not player.scoring_lineup_slots.exists()
    )


def _prune_stray_duplicates(league, name, keep):
    """Delete empty duplicate player records that share ``name`` with ``keep``.
    Hand-renaming a spot used to leave one of these behind every time."""
    removed = 0
    for other in Player.objects.filter(league=league, name__iexact=name).exclude(pk=keep.pk):
        if _is_unused(other):
            other.delete()
            removed += 1
    return removed


@transaction.atomic
def record_player_change(slot, taking_over, took_over_on, left=''):
    """Apply a takeover of ``slot`` (a one-pocket Team).

    ``taking_over`` is the new player's name. If it differs from the slot's
    name, the slot is renamed and the previous occupant leaves the roster.
    If the slot was already renamed by hand (the common case today), pass the
    departing player's name as ``left`` so their earlier matches can be
    credited to them. Results whose score was entered before ``took_over_on``
    stay with the departing player. Returns (old_player, new_player, reassigned_count).
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
        new = _find_or_create_player(league, taking_over)
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
            new = _find_or_create_player(league, taking_over)
            new.team = slot
            new.save(update_fields=['team'])
        old = _find_or_create_player(league, left)
        if old.team_id == slot.id and old.id != new.id:
            old.team = None
            old.save(update_fields=['team'])

    # Hand renames left empty duplicates behind (a second "Sysco" with no
    # matches); they break later team saves, so clear them while we're here.
    _prune_stray_duplicates(league, new.name, new)
    if old is not None:
        _prune_stray_duplicates(league, old.name, old)

    reassigned = 0
    results = (
        MatchResult.objects.filter(match__week__season__league=league)
        .exclude(match__week__season__status=Season.Status.ARCHIVED)
        .filter(Q(match__home_team=slot) | Q(match__away_team=slot))
        .select_related('match__week')
    )
    for result in results:
        # Go by when the score was entered, not the scheduled week: a makeup
        # played late belongs to whoever held the spot when it was played.
        played_on = timezone.localtime(result.created_at).date() if result.created_at else result.match.week.date
        player = old if played_on < took_over_on else new
        if player is None:
            continue
        field = 'home_player' if result.match.home_team_id == slot.id else 'away_player'
        if getattr(result, f'{field}_id') != player.id:
            setattr(result, field, player)
            result.save(update_fields=[field, 'updated_at'])
            reassigned += 1

    return old, new, reassigned
