from datetime import date

from django import forms
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from core.models import League, Team
from .models import Holiday, Match, Season, Week
from .services import (
    archive_season,
    create_mirrored_season_schedule,
    create_new_playable_week_at_end,
    get_next_start_dates,
    get_valid_destination_weeks,
    move_match_to_week,
    rebalance_season_matches,
    recreate_season_schedule,
)


def get_user_league(request):
    if request.user.is_superuser:
        return None

    access = getattr(request.user, 'league_admin_access', None)
    return access.league if access else None


class LeagueScopedSeasonFilter(admin.SimpleListFilter):
    title = 'season'
    parameter_name = 'season'

    def lookups(self, request, model_admin):
        queryset = Season.objects.all().order_by('name')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            queryset = queryset.filter(league=user_league)

        return [(season.pk, str(season)) for season in queryset]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(season_id=self.value())
        return queryset


class LeagueScopedWeekFilter(admin.SimpleListFilter):
    title = 'week'
    parameter_name = 'week'

    def lookups(self, request, model_admin):
        queryset = Week.objects.select_related('season').all().order_by('date')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            queryset = queryset.filter(season__league=user_league)

        return [(week.pk, str(week)) for week in queryset]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(week_id=self.value())
        return queryset


class LeagueScopedAdminMixin:
    league_field_name = 'league'

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        user_league = get_user_league(request)

        if request.user.is_superuser or user_league is None:
            return queryset

        return queryset.filter(**{self.league_field_name: user_league})

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            initial[self.league_field_name] = user_league.pk

        return initial

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change=change, **kwargs)
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            league_field = form.base_fields.get(self.league_field_name)
            if league_field:
                league_field.initial = user_league.pk
                league_field.widget = forms.HiddenInput()

        return form

    def save_model(self, request, obj, form, change):
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            setattr(obj, self.league_field_name, user_league)

        super().save_model(request, obj, form, change)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            if db_field.name == 'league':
                kwargs['queryset'] = League.objects.filter(pk=user_league.pk)
                kwargs['initial'] = user_league.pk

        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class WeekInline(admin.TabularInline):
    model = Week
    extra = 0
    fields = ('date', 'number', 'notes')
    ordering = ('date',)
    show_change_link = True


class MatchInline(admin.TabularInline):
    model = Match
    extra = 0
    fields = ('sort_order', 'home_team', 'away_team', 'location')
    ordering = ('sort_order',)


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ('date', 'description')
    search_fields = ('description',)
    ordering = ('date',)


@admin.register(Season)
class SeasonAdmin(LeagueScopedAdminMixin, admin.ModelAdmin):
    change_form_template = 'admin/scheduling/season/change_form.html'
    list_display = ('name', 'league', 'status')
    search_fields = ('name', 'league__name')
    ordering = ('league__name', 'name')
    inlines = [WeekInline]

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('league', 'status')
        return ('status',)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/recreate-schedule/',
                self.admin_site.admin_view(self.recreate_schedule_view),
                name='scheduling_season_recreate_schedule',
            ),
            path(
                '<path:object_id>/mirror-schedule/',
                self.admin_site.admin_view(self.mirror_schedule_view),
                name='scheduling_season_mirror_schedule',
            ),
            path(
                '<path:object_id>/move-live/',
                self.admin_site.admin_view(self.move_live_view),
                name='scheduling_season_move_live',
            ),
            path(
                '<path:object_id>/archive/',
                self.admin_site.admin_view(self.archive_season_view),
                name='scheduling_season_archive',
            ),
            path(
                '<path:object_id>/rebalance-schedule/',
                self.admin_site.admin_view(self.rebalance_schedule_view),
                name='scheduling_season_rebalance_schedule',
            ),
            path(
                '<path:object_id>/swap-match/<int:match_id>/',
                self.admin_site.admin_view(self.swap_match_view),
                name='scheduling_season_swap_match',
            ),
            path(
                '<path:object_id>/update-match-location/<int:match_id>/',
                self.admin_site.admin_view(self.update_match_location_view),
                name='scheduling_season_update_match_location',
            ),
            path(
                '<path:object_id>/move-match/<int:match_id>/',
                self.admin_site.admin_view(self.move_match_view),
                name='scheduling_season_move_match',
            ),
        ]
        return custom_urls + urls

    def _get_league_scoped_object(self, request, object_id):
        queryset = self.get_queryset(request)
        return queryset.get(pk=object_id)

    def _get_start_date_choices(self, season):
        start_dates = get_next_start_dates(season.league, date.today(), count=5)
        return [
            {
                'value': start_date.isoformat(),
                'label': start_date.strftime('%A, %B %d, %Y'),
            }
            for start_date in start_dates
        ]

    def _get_schedule_data(self, season):
        weeks = list(
            season.weeks.prefetch_related(
                'matches__home_team__venue',
                'matches__away_team__venue',
                'matches__result',
            ).order_by('date', 'number')
        )

        for week in weeks:
            for match in week.matches.all():
                match.valid_destination_weeks = get_valid_destination_weeks(season, match)
                match.has_result = hasattr(match, 'result')

        return weeks

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}

        if object_id:
            season = self._get_league_scoped_object(request, object_id)
            if season:
                league = season.league
            else:
                league = None

            extra_context['start_date_choices'] = self._get_start_date_choices(season)
            extra_context['recreate_schedule_url'] = reverse(
                'admin:scheduling_season_recreate_schedule',
                args=[season.pk],
            )
            extra_context['mirror_schedule_url'] = reverse(
                'admin:scheduling_season_mirror_schedule',
                args=[season.pk],
            )
            extra_context['rebalance_schedule_url'] = reverse(
                'admin:scheduling_season_rebalance_schedule',
                args=[season.pk],
            )
            extra_context['schedule_weeks'] = self._get_schedule_data(season)
            extra_context['swap_match_url_name'] = 'admin:scheduling_season_swap_match'
            extra_context['update_match_location_url_name'] = 'admin:scheduling_season_update_match_location'
            extra_context['move_match_url_name'] = 'admin:scheduling_season_move_match'
            extra_context['league'] = league

            if season.status == Season.Status.WORKING:
                extra_context['move_live_url'] = reverse(
                    'admin:scheduling_season_move_live',
                    args=[season.pk],
                )

            if season.status == Season.Status.ACTIVE:
                extra_context['archive_season_url'] = reverse(
                    'admin:scheduling_season_archive',
                    args=[season.pk],
                )

        return super().changeform_view(
            request,
            object_id=object_id,
            form_url=form_url,
            extra_context=extra_context,
        )

    def archive_season_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        try:
            archived_season = archive_season(season)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        self.message_user(
            request,
            f'Season archived successfully as "{archived_season.name}".',
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse('admin:index'))

    def recreate_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        start_date_value = request.POST.get('start_date')
        if not start_date_value:
            self.message_user(request, 'Please choose a start date before recreating the schedule.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        try:
            start_date = date.fromisoformat(start_date_value)
        except ValueError:
            self.message_user(request, 'Invalid start date selected.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        created_weeks = recreate_season_schedule(season, start_date=start_date)

        self.message_user(
            request,
            f'Schedule recreated successfully with {len(created_weeks)} week(s).',
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def mirror_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        created_weeks = create_mirrored_season_schedule(season)

        self.message_user(
            request,
            f'Mirrored schedule created successfully with {len(created_weeks)} week(s).',
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def move_live_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        if season.status != Season.Status.WORKING:
            self.message_user(request, 'Only a working season can be moved live.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        Season.objects.filter(
            league=season.league,
            status=Season.Status.ACTIVE,
        ).exclude(pk=season.pk).update(status=Season.Status.WORKING)

        season.status = Season.Status.ACTIVE
        season.save(update_fields=['status'])

        self.message_user(request, 'Season moved live successfully.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def rebalance_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        moved_matches = rebalance_season_matches(season)

        self.message_user(
            request,
            f'Rebalanced schedule successfully. Moved {len(moved_matches)} match(es).',
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def swap_match_view(self, request, object_id, match_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        match = get_object_or_404(
            Match.objects.select_related('week__season', 'home_team__venue', 'away_team__venue'),
            pk=match_id,
            week__season=season,
        )

        original_home = match.home_team
        original_away = match.away_team
        new_home = original_away

        existing_home_matches_at_venue = match.week.matches.filter(
            home_team__venue=new_home.venue,
        ).exclude(pk=match.pk).count()

        if existing_home_matches_at_venue >= new_home.venue.max_home_teams:
            self.message_user(
                request,
                f'Cannot swap teams because venue "{new_home.venue.name}" would exceed its max home teams limit for this week.',
                level=messages.ERROR,
            )
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        match.home_team = original_away
        match.away_team = original_home

        if not request.POST.get('keep_location'):
            match.location = original_away.venue.name

        match.full_clean()
        match.save()

        self.message_user(request, 'Match home and away teams were swapped.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def update_match_location_view(self, request, object_id, match_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        match = get_object_or_404(
            Match.objects.select_related('week__season'),
            pk=match_id,
            week__season=season,
        )

        match.location = request.POST.get('location', '').strip()
        match.full_clean()
        match.save(update_fields=['location'])

        self.message_user(request, 'Match location updated.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def move_match_view(self, request, object_id, match_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        match = get_object_or_404(
            Match.objects.select_related('week__season', 'home_team__venue'),
            pk=match_id,
            week__season=season,
        )

        target_week_id = request.POST.get('target_week')
        if not target_week_id:
            self.message_user(request, 'Please choose a destination week.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        try:
            if target_week_id == 'new':
                target_week = create_new_playable_week_at_end(season)
            else:
                target_week = get_object_or_404(
                    Week,
                    pk=target_week_id,
                    season=season,
                )

            move_match_to_week(match, target_week)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

        self.message_user(request, 'Match moved successfully.', level=messages.SUCCESS)
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

@admin.register(Week)
class WeekAdmin(admin.ModelAdmin):
    list_display = ('season', 'date', 'number', 'match_count')
    search_fields = ('season__name', 'season__league__name', 'notes')
    ordering = ('season', 'date')
    inlines = [MatchInline]

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('season__league', LeagueScopedSeasonFilter)
        return (LeagueScopedSeasonFilter,)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        user_league = get_user_league(request)

        if request.user.is_superuser or user_league is None:
            return queryset

        return queryset.filter(season__league=user_league)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            if db_field.name == 'season':
                kwargs['queryset'] = Season.objects.filter(league=user_league).order_by('name')

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description='Matches')
    def match_count(self, obj):
        return obj.matches.count()


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ('week', 'home_team', 'away_team', 'location', 'enter_score_link')
    search_fields = ('home_team__name', 'away_team__name')
    ordering = ('week', 'sort_order')

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('week__season__league', LeagueScopedWeekFilter)
        return (LeagueScopedWeekFilter,)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        user_league = get_user_league(request)

        if request.user.is_superuser or user_league is None:
            return queryset

        return queryset.filter(week__season__league=user_league)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            if db_field.name == 'week':
                kwargs['queryset'] = Week.objects.filter(season__league=user_league).order_by('date')
            elif db_field.name in ('home_team', 'away_team'):
                kwargs['queryset'] = Team.objects.filter(league=user_league).order_by('name')

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description='Score Entry')
    def enter_score_link(self, obj):
        url = reverse('admin:results_matchresult_enter_score', args=[obj.pk])
        return format_html('<a href="{}">Enter Score</a>', url)