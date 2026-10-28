"""Remove leftover substitute players from a league.

A sub is any player whose name contains "(SUB)" in any mix of case, with or
without spaces inside the brackets: "(SUB)", "(sub)", "(Sub)", "( sub )".

A sub is only removed when nothing else would go with it. Deleting a player
also deletes their per-match scores, tournament entries and scoring records,
so any sub that still has one of those is kept, as is any sub still on a team
roster. Links that are merely cleared on delete (archived season rows,
captaincy, who-played-for-which-side on a result) don't block removal; the
archive rows keep the name, only the link to the live player goes.
"""
import re

from django.db import router, transaction
from django.db.models.deletion import Collector

from .models import Player

SUB_PATTERN = re.compile(r'\(\s*sub\s*\)', re.IGNORECASE)


def is_sub(name):
    return bool(SUB_PATTERN.search(name or ''))


def _cascade_blockers(player):
    """Records that deleting ``player`` would delete with it, as {label: count}."""
    collector = Collector(using=router.db_for_write(Player))
    collector.collect([player])
    blockers = {}
    for model, instances in collector.data.items():
        if model is Player:
            continue
        label = str(model._meta.verbose_name_plural)
        blockers[label] = blockers.get(label, 0) + len(instances)
    for qs in collector.fast_deletes:
        count = qs.count()
        if count:
            label = str(qs.model._meta.verbose_name_plural)
            blockers[label] = blockers.get(label, 0) + count
    return blockers


def plan_sub_cleanup(league):
    """Return (removable, kept): lists of dicts with the player and, for kept
    ones, the reasons they can't be removed."""
    removable = []
    kept = []
    subs = [
        p for p in Player.objects.filter(league=league).select_related('team').order_by('name')
        if is_sub(p.name)
    ]
    for player in subs:
        reasons = []
        if player.team_id:
            reasons.append(f'on the {player.team.name} roster')
        for label, count in sorted(_cascade_blockers(player).items()):
            reasons.append(f'{count} {label}')
        entry = {'player': player, 'reasons': reasons}
        (kept if reasons else removable).append(entry)
    return removable, kept


def remove_subs(league):
    """Delete the removable subs. The plan is rebuilt inside the transaction so
    anything that changed since the confirmation page was shown is respected.
    Returns the names of the players removed."""
    with transaction.atomic():
        removable, _ = plan_sub_cleanup(league)
        names = [entry['player'].name for entry in removable]
        Player.objects.filter(pk__in=[entry['player'].pk for entry in removable]).delete()
    return names
