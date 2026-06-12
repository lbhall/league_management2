from django.contrib import admin
from .models import Tournament, TournamentPlayer

from django.utils.html import format_html
from django.urls import reverse

def get_user_league(request):
    if request.user.is_superuser:
        return None
    access = getattr(request.user, 'league_admin_access', None)
    return access.league if access else None

@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    list_display = ('season', 'created_at', 'manage_players_link')

    def manage_players_link(self, obj):
        url = reverse('tournament_players') + f'?league={obj.season.league_id}'
        return format_html('<a href="{}">Manage Tournament Players</a>', url)
    manage_players_link.short_description = 'Management'

    def has_module_permission(self, request):
        return request.user.is_staff

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if request.user.is_staff:
            if obj is None:
                return True
            league = get_user_league(request)
            if league:
                return obj.season.league == league
            return True # If staff but no specific league assigned, let them see it? 
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        league = get_user_league(request)
        if league:
            return qs.filter(season__league=league)
        return qs

@admin.register(TournamentPlayer)
class TournamentPlayerAdmin(admin.ModelAdmin):
    list_display = ('player', 'tournament', 'manage_players_link')
    list_filter = ('tournament',)

    def manage_players_link(self, obj):
        url = reverse('tournament_players') + f'?league={obj.tournament.season.league_id}'
        return format_html('<a href="{}">Tournament Management Page</a>', url)
    manage_players_link.short_description = 'Page'

    def has_module_permission(self, request):
        return request.user.is_staff

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if request.user.is_staff:
            if obj is None:
                return True
            league = get_user_league(request)
            if league:
                return obj.tournament.season.league == league
            return True
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        league = get_user_league(request)
        if league:
            return qs.filter(tournament__season__league=league)
        return qs
