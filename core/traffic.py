"""Hits and unique visitors for the public site, reported inside the admin."""
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from .models import League, PageView


def visible_leagues(user):
    """Leagues a staff user may see traffic for: all for superusers and
    unscoped staff, just their own for league-scoped admins."""
    access = getattr(user, 'league_admin_access', None)
    if user.is_superuser or access is None:
        return list(League.objects.order_by('name')), None
    return [access.league], access.league


def build_traffic_report(user, requested_league_id=None, include_staff=False):
    league_options, forced_league = visible_leagues(user)

    selected_league = None
    if requested_league_id:
        selected_league = next(
            (lg for lg in league_options if str(lg.id) == str(requested_league_id)), None,
        )
    if selected_league is None and forced_league is not None:
        selected_league = forced_league
    if selected_league is None and len(league_options) == 1:
        selected_league = league_options[0]

    # Staff browsing is recorded but, by default, not counted. The report can
    # include it so an operator can check the counter with their own visits.
    all_views = PageView.objects.all()
    if selected_league is not None:
        all_views = all_views.filter(league=selected_league)
    staff_views = all_views.filter(is_staff=True)
    views = all_views if include_staff else all_views.filter(is_staff=False)

    today = timezone.localdate()

    def summarise(queryset):
        hits = queryset.count()
        # Visitor keys rotate daily, so "unique" means per device per day.
        uniques = queryset.values('day', 'visitor_key').distinct().count()
        return hits, uniques

    cards = []
    for label, since in (
        ('Today', today),
        ('Last 7 days', today - timedelta(days=6)),
        ('Last 30 days', today - timedelta(days=29)),
        ('All time', None),
    ):
        subset = views if since is None else views.filter(day__gte=since)
        hits, uniques = summarise(subset)
        cards.append({'label': label, 'hits': hits, 'uniques': uniques})

    month = views.filter(day__gte=today - timedelta(days=29))
    daily = [
        {'day': row['day'], 'hits': row['hits'], 'uniques': row['uniques']}
        for row in month.values('day')
        .annotate(hits=Count('id'), uniques=Count('visitor_key', distinct=True))
        .order_by('-day')
    ]
    top_pages = [
        {'path': row['path'], 'hits': row['hits'], 'uniques': row['uniques']}
        for row in month.values('path')
        .annotate(hits=Count('id'), uniques=Count('visitor_key', distinct=True))
        .order_by('-hits', 'path')[:15]
    ]

    return {
        'league_options': league_options,
        'selected_league': selected_league,
        'include_staff': include_staff,
        'staff_hits': staff_views.count(),
        'cards': cards,
        'daily': daily,
        'top_pages': top_pages,
    }
