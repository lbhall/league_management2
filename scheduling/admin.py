from datetime import date

from django import forms
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.template.response import TemplateResponse

from core.models import League, Player, Team
from .models import (
    ArchivedMatch,
    ArchivedPlayer,
    ArchivedPlayerMatchResult,
    ArchivedSeason,
    ArchivedTeam,
    Holiday,
    Match,
    Season,
    Week,
)
from .services import (
    archive_season,
    create_mirrored_season_schedule,
    create_new_playable_week_at_end,
    delete_week,
    get_next_start_dates,
    link_archived_season_by_name,
    get_valid_destination_weeks,
    get_venue_violations,
    is_schedule_mirrored,
    move_match_to_week,
    move_week_down,
    move_week_up,
    pack_season_schedule,
    rebalance_season_matches,
    recreate_season_schedule,
    renumber_weeks,
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
    list_display = ('name', 'league', 'status', 'tournament_players_link')
    list_filter = ('status',)
    search_fields = ('name', 'league__name')
    ordering = ('league__name', 'name')
    inlines = [WeekInline]

    def tournament_players_link(self, obj):
        url = reverse('tournament_players') + f'?league={obj.league_id}'
        return format_html('<a href="{}">Tournament Players</a>', url)
    tournament_players_link.short_description = 'Tournaments'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'schedule/',
                self.admin_site.admin_view(self.schedule_redirect_view),
                name='scheduling_schedule',
            ),
            path(
                '<path:object_id>/manage-schedule/',
                self.admin_site.admin_view(self.manage_schedule_view),
                name='scheduling_season_manage_schedule',
            ),
            path(
                '<path:object_id>/manage-schedule/',
                self.admin_site.admin_view(self.manage_schedule_view),
                name='scheduling_season_manage_schedule',
            ),
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
                '<path:object_id>/compress-schedule/',
                self.admin_site.admin_view(self.compress_schedule_view),
                name='scheduling_season_compress_schedule',
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
            path(
                '<path:object_id>/move-week/<int:week_id>/up/',
                self.admin_site.admin_view(self.move_week_up_view),
                name='scheduling_season_move_week_up',
            ),
            path(
                '<path:object_id>/move-week/<int:week_id>/down/',
                self.admin_site.admin_view(self.move_week_down_view),
                name='scheduling_season_move_week_down',
            ),
            path(
                '<path:object_id>/delete-week/<int:week_id>/',
                self.admin_site.admin_view(self.delete_week_view),
                name='scheduling_season_delete_week',
            ),
            path(
                '<path:object_id>/renumber-weeks/',
                self.admin_site.admin_view(self.renumber_weeks_view),
                name='scheduling_season_renumber_weeks',
            ),
        ]
        return custom_urls + urls

    def schedule_redirect_view(self, request):
        user_league = get_user_league(request)

        if user_league is not None:
            season = Season.objects.filter(
                league=user_league,
                status=Season.Status.ACTIVE,
            ).order_by('id').first()

            if season:
                return HttpResponseRedirect(
                    reverse('admin:scheduling_season_manage_schedule', args=[season.pk])
                )

            return HttpResponseRedirect(reverse('admin:scheduling_season_changelist'))

        leagues = League.objects.order_by('name')
        league_rows = []
        for league in leagues:
            season = Season.objects.filter(
                league=league,
                status=Season.Status.ACTIVE,
            ).order_by('id').first()

            if season:
                league_rows.append({
                    'league': league,
                    'url': reverse('admin:scheduling_season_manage_schedule', args=[season.pk]),
                })

        return TemplateResponse(
            request,
            'admin/scheduling/schedule_selector.html',
            {
                **self.admin_site.each_context(request),
                'title': 'Choose a League Schedule',
                'league_rows': league_rows,
            },
        )

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
        teams = [
            team for team in season.league.teams.order_by('name')
            if team.name.strip().upper() != 'BYE'
        ]

        for index, week in enumerate(weeks):
            scheduled_team_ids = set()
            display_matches = []
            teams_playing_bye = set()

            for match in week.matches.all():
                match.valid_destination_weeks = get_valid_destination_weeks(season, match)
                result = getattr(match, 'result', None)
                if result and season.league.results_type == 'one_pocket':
                    match.has_result = (
                        result.home_team_score is not None
                        and result.away_team_score is not None
                        and max(result.home_team_score, result.away_team_score) >= 3
                    )
                else:
                    match.has_result = result is not None
                scheduled_team_ids.add(match.home_team_id)
                scheduled_team_ids.add(match.away_team_id)

                home_is_bye = match.home_team.name.strip().upper() == 'BYE'
                away_is_bye = match.away_team.name.strip().upper() == 'BYE'

                if home_is_bye or away_is_bye:
                    # The "BYE" placeholder team isn't a real opponent, so
                    # surface its real opponent in the bye-teams list below
                    # instead of rendering a score-entry row for it.
                    teams_playing_bye.add((match.away_team if home_is_bye else match.home_team).id)
                    continue

                display_matches.append(match)

            week.display_matches = display_matches
            week.bye_teams = [
                team for team in teams
                if team.id not in scheduled_team_ids or team.id in teams_playing_bye
            ]
            if week.number is None:
                week.can_move_up = index > 0
                week.can_move_down = index < len(weeks) - 1
            else:
                week.can_move_up = any(w.number is not None for w in weeks[:index])
                week.can_move_down = any(w.number is not None for w in weeks[index + 1:])
            week.can_delete = week.number is not None and not week.matches.all()

        return weeks

    def _get_redirect_url(self, request, season):
        next_url = request.POST.get('next')
        if next_url:
            return HttpResponseRedirect(next_url)
        return HttpResponseRedirect(reverse('admin:scheduling_season_change', args=[season.pk]))

    def manage_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)
        league = season.league

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'season': season,
            'league': league,
            'schedule_weeks': self._get_schedule_data(season),
            'all_teams': list(league.teams.order_by('name')),
            'start_date_choices': self._get_start_date_choices(season),
            'venue_violations': get_venue_violations(season),
            'swap_match_url_name': 'admin:scheduling_season_swap_match',
            'update_match_location_url_name': 'admin:scheduling_season_update_match_location',
            'move_match_url_name': 'admin:scheduling_season_move_match',
            'is_archived': season.status == Season.Status.ARCHIVED,
        }

        if season.status != Season.Status.ARCHIVED:
            # Schedule-shaping tools only make sense before the season is archived.
            context['mirror_schedule_url'] = reverse('admin:scheduling_season_mirror_schedule', args=[season.pk])
            context['schedule_mirrored'] = is_schedule_mirrored(season)
            context['rebalance_schedule_url'] = reverse('admin:scheduling_season_rebalance_schedule', args=[season.pk])
            context['renumber_weeks_url'] = reverse('admin:scheduling_season_renumber_weeks', args=[season.pk])

        if season.status == Season.Status.WORKING:
            context['recreate_schedule_url'] = reverse('admin:scheduling_season_recreate_schedule', args=[season.pk])
            context['move_live_url'] = reverse('admin:scheduling_season_move_live', args=[season.pk])
            context['compress_schedule_url'] = reverse('admin:scheduling_season_compress_schedule', args=[season.pk])

        if season.status == Season.Status.ACTIVE:
            context['archive_season_url'] = reverse('admin:scheduling_season_archive', args=[season.pk])
            context['archive_button_label'] = 'Archive Season'
        elif season.status == Season.Status.ARCHIVED:
            context['archive_season_url'] = reverse('admin:scheduling_season_archive', args=[season.pk])
            context['archive_button_label'] = 'Rebuild Archive'

        return TemplateResponse(
            request,
            'admin/scheduling/season/manage_schedule.html',
            context
        )

    def move_week_up_view(self, request, object_id, week_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        week = get_object_or_404(Week, pk=week_id, season=season)

        try:
            move_week_up(week)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return self._get_redirect_url(request, season)

        self.message_user(request, 'Week moved up successfully.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

    def move_week_down_view(self, request, object_id, week_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        week = get_object_or_404(Week, pk=week_id, season=season)

        try:
            move_week_down(week)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return self._get_redirect_url(request, season)

        self.message_user(request, 'Week moved down successfully.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

    def delete_week_view(self, request, object_id, week_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        week = get_object_or_404(Week, pk=week_id, season=season)

        try:
            delete_week(week)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return self._get_redirect_url(request, season)

        self.message_user(request, 'Week deleted successfully.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

    def renumber_weeks_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        count = renumber_weeks(season)
        self.message_user(
            request,
            f'Renumbered {count} week(s).',
            level=messages.SUCCESS,
        )
        return self._get_redirect_url(request, season)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        return super().changeform_view(
            request,
            object_id=object_id,
            form_url=form_url,
            extra_context=extra_context,
        )

    def archive_season_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        rebuilding = season.status == Season.Status.ARCHIVED

        try:
            archived_season = archive_season(season)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return self._get_redirect_url(request, season)

        if rebuilding:
            self.message_user(
                request,
                f'Archive rebuilt successfully as "{archived_season.name}".',
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(
                reverse('admin:scheduling_archivedseason_change', args=[archived_season.pk])
            )

        self.message_user(
            request,
            f'Season archived successfully as "{archived_season.name}".',
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse('admin:index'))

    def recreate_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        if season.status != Season.Status.WORKING:
            self.message_user(
                request,
                'Only a working season\'s schedule can be recreated.',
                level=messages.ERROR,
            )
            return self._get_redirect_url(request, season)

        start_date_value = request.POST.get('start_date')
        if not start_date_value:
            self.message_user(request, 'Please choose a start date before recreating the schedule.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

        try:
            start_date = date.fromisoformat(start_date_value)
        except ValueError:
            self.message_user(request, 'Invalid start date selected.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

        created_weeks = recreate_season_schedule(season, start_date=start_date)

        self.message_user(
            request,
            f'Schedule recreated successfully with {len(created_weeks)} week(s).',
            level=messages.SUCCESS,
        )
        return self._get_redirect_url(request, season)

    def mirror_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        if season.status == Season.Status.ARCHIVED:
            self.message_user(request, 'Cannot mirror the schedule of an archived season.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

        if is_schedule_mirrored(season):
            self.message_user(
                request,
                'This schedule has already been mirrored: every pairing already has its return match.',
                level=messages.ERROR,
            )
            return self._get_redirect_url(request, season)

        created_weeks = create_mirrored_season_schedule(season)

        self.message_user(
            request,
            f'Mirrored schedule created successfully with {len(created_weeks)} week(s).',
            level=messages.SUCCESS,
        )
        return self._get_redirect_url(request, season)

    def move_live_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        if season.status != Season.Status.WORKING:
            self.message_user(request, 'Only a working season can be moved live.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

        Season.objects.filter(
            league=season.league,
            status=Season.Status.ACTIVE,
        ).exclude(pk=season.pk).update(status=Season.Status.WORKING)

        season.status = Season.Status.ACTIVE
        season.save(update_fields=['status'])

        self.message_user(request, 'Season moved live successfully.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

    def compress_schedule_view(self, request, object_id):
        """Repack a working season into the fewest weeks the venues allow.
        Matchups, home/away and locations are untouched; only the week each
        match lands in changes, and empty weeks at the end are deleted."""
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        if season.status != Season.Status.WORKING:
            self.message_user(request, 'Only a working season can be compressed.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

        before = season.weeks.filter(number__isnull=False).count()
        try:
            result = pack_season_schedule(season, random_seed=1, attempts=20000)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return self._get_redirect_url(request, season)

        after = season.weeks.filter(number__isnull=False).count()
        if result['deleted_weeks']:
            self.message_user(
                request,
                f'Schedule compressed from {before} to {after} play weeks '
                f'({result["moved"]} match(es) moved).',
                level=messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f'The schedule is already as short as the venues allow ({after} play weeks).',
                level=messages.INFO,
            )
        return self._get_redirect_url(request, season)

    def rebalance_schedule_view(self, request, object_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        if season.status == Season.Status.ARCHIVED:
            self.message_user(request, 'Cannot rebalance the schedule of an archived season.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

        moved_matches = rebalance_season_matches(season)

        self.message_user(
            request,
            f'Rebalanced schedule successfully. Moved {len(moved_matches)} match(es).',
            level=messages.SUCCESS,
        )
        return self._get_redirect_url(request, season)

    def swap_match_view(self, request, object_id, match_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

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
            return self._get_redirect_url(request, season)

        match.home_team = original_away
        match.away_team = original_home

        if not request.POST.get('keep_location'):
            match.location = original_away.venue.name

        match.full_clean()
        match.save()

        self.message_user(request, 'Match home and away teams were swapped.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

    def update_match_location_view(self, request, object_id, match_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        match = get_object_or_404(
            Match.objects.select_related('week__season'),
            pk=match_id,
            week__season=season,
        )

        match.location = request.POST.get('location', '').strip()
        match.full_clean()
        match.save(update_fields=['location'])

        self.message_user(request, 'Match location updated.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

    def move_match_view(self, request, object_id, match_id):
        season = self._get_league_scoped_object(request, object_id)

        if request.method != 'POST':
            return self._get_redirect_url(request, season)

        match = get_object_or_404(
            Match.objects.select_related('week__season', 'home_team__venue'),
            pk=match_id,
            week__season=season,
        )

        target_week_id = request.POST.get('target_week')
        if not target_week_id:
            self.message_user(request, 'Please choose a destination week.', level=messages.ERROR)
            return self._get_redirect_url(request, season)

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
            return self._get_redirect_url(request, season)

        self.message_user(request, 'Match moved successfully.', level=messages.SUCCESS)
        return self._get_redirect_url(request, season)

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

class LeagueScopedArchivedSeasonFilter(admin.SimpleListFilter):
    title = 'archived season'
    parameter_name = 'archived_season'

    def lookups(self, request, model_admin):
        queryset = ArchivedSeason.objects.select_related('league').order_by('-archived_at', '-id')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            queryset = queryset.filter(league=user_league)

        return [(season.pk, str(season)) for season in queryset]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(archived_season_id=self.value())
        return queryset


class LeagueScopedLinkInlineMixin:
    """Limit the live ``player``/``team`` link choices to the staff member's
    own league. The autocomplete endpoint is already scoped through the
    Player/Team admins; this also rejects a hand-crafted POST carrying
    another league's id."""

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            if db_field.name == 'player':
                kwargs['queryset'] = Player.objects.filter(league=user_league).order_by('name')
            elif db_field.name == 'team':
                kwargs['queryset'] = Team.objects.filter(league=user_league).order_by('name')

        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class ArchivedTeamInline(LeagueScopedLinkInlineMixin, admin.TabularInline):
    model = ArchivedTeam
    extra = 0
    fields = (
        'league_rank', 'team_name', 'team', 'team_rank', 'captain_name', 'venue_name',
        'matches_won', 'matches_lost', 'games_won', 'games_lost',
    )
    autocomplete_fields = ('team',)
    ordering = ('league_rank', 'team_name')


class ArchivedPlayerInline(LeagueScopedLinkInlineMixin, admin.TabularInline):
    model = ArchivedPlayer
    extra = 0
    fields = (
        'player_name', 'player', 'team_name', 'male',
        'games_won', 'games_lost', 'run_outs', 'eight_on_the_breaks', 'sweeps',
    )
    autocomplete_fields = ('player',)
    ordering = ('team_name', 'player_name')


@admin.register(ArchivedSeason)
class ArchivedSeasonAdmin(LeagueScopedAdminMixin, admin.ModelAdmin):
    """Archives are created by "Archive Season"; this admin exists to correct
    names and numbers after the fact (and to inspect what was captured)."""

    list_display = ('name', 'league', 'results_type', 'start_date', 'end_date', 'archived_at', 'matches_link')
    search_fields = ('name', 'league__name', 'teams__team_name', 'players__player_name')
    ordering = ('-archived_at', '-id')
    fields = ('league', 'name', 'season', 'results_type', 'team_size', 'start_date', 'end_date', 'archived_at')
    readonly_fields = ('season', 'archived_at')
    inlines = [ArchivedTeamInline, ArchivedPlayerInline]
    actions = ['link_by_name']

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('league', 'results_type')
        return ('results_type',)

    def has_add_permission(self, request):
        return False

    @admin.action(description='Link archived teams/players to current records by name')
    def link_by_name(self, request, queryset):
        teams = players = results = 0
        for archived_season in queryset:
            t, p, r = link_archived_season_by_name(archived_season)
            teams += t
            players += p
            results += r
        self.message_user(
            request,
            f'Linked {teams} team(s), {players} player(s) and {results} match line(s). '
            'Rows whose name is missing or duplicated in the league were left for manual linking.',
            level=messages.SUCCESS,
        )

    @admin.display(description='Matches')
    def matches_link(self, obj):
        count = obj.matches.count()
        if not count:
            return '0'
        url = reverse('admin:scheduling_archivedmatch_changelist') + f'?archived_season={obj.pk}'
        return format_html('<a href="{}">{}</a>', url, count)


class ArchivedPlayerMatchResultInline(LeagueScopedLinkInlineMixin, admin.TabularInline):
    model = ArchivedPlayerMatchResult
    extra = 0
    fields = ('player_name', 'player', 'team_name', 'wins', 'losses', 'runouts', 'eight_on_the_breaks', 'won_all_games')
    autocomplete_fields = ('player',)
    ordering = ('team_name', 'player_name')


@admin.register(ArchivedMatch)
class ArchivedMatchAdmin(admin.ModelAdmin):
    list_display = (
        'date', 'week_number', 'archived_season', 'home_team_name', 'home_team_score',
        'away_team_score', 'away_team_name', 'race_label', 'completed',
    )
    list_select_related = ('archived_season', 'archived_season__league')
    search_fields = ('home_team_name', 'away_team_name', 'archived_season__name')
    ordering = ('-date', 'sort_order')
    inlines = [ArchivedPlayerMatchResultInline]

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('archived_season__league', LeagueScopedArchivedSeasonFilter, 'completed')
        return (LeagueScopedArchivedSeasonFilter, 'completed')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        user_league = get_user_league(request)

        if request.user.is_superuser or user_league is None:
            return queryset

        return queryset.filter(archived_season__league=user_league)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        user_league = get_user_league(request)

        if not request.user.is_superuser and user_league is not None:
            if db_field.name == 'archived_season':
                kwargs['queryset'] = ArchivedSeason.objects.filter(league=user_league)

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_add_permission(self, request):
        return False
