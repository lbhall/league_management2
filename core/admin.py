from decimal import Decimal

from django import forms
from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse

from .models import League, LeagueAdminAccess, Player, Team, Venue
from scheduling.models import Season

def get_user_league(request):
    if request.user.is_superuser:
        return None

    access = getattr(request.user, 'league_admin_access', None)
    return access.league if access else None


class LeagueScopedAdminMixin:
    league_field_name = 'league'

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        user_league = get_user_league(request)

        if request.user.is_superuser or user_league is None:
            return queryset

        return queryset.filter(**{self.league_field_name: user_league})

    def get_exclude(self, request, obj=None):
        return super().get_exclude(request, obj)

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
            elif db_field.name == 'venue':
                kwargs['queryset'] = Venue.objects.filter(league=user_league).order_by('name')
            elif db_field.name == 'team':
                kwargs['queryset'] = Team.objects.filter(league=user_league).order_by('name')
            elif db_field.name == 'captain':
                kwargs['queryset'] = Player.objects.filter(league=user_league).order_by('name')

        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class TeamAdminForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['venue'].queryset = Venue.objects.none()
        self.fields['captain'].queryset = Player.objects.none()

        league_id = None

        if self.is_bound:
            league_id = self.data.get('league') or None
        elif self.instance and self.instance.pk:
            league_id = self.instance.league_id
        elif self.initial.get('league'):
            league_id = self.initial.get('league')

        if league_id:
            self.fields['venue'].queryset = Venue.objects.filter(
                league_id=league_id,
            ).order_by('name')

        if self.instance and self.instance.pk:
            self.fields['captain'].queryset = Player.objects.filter(
                team=self.instance,
            ).order_by('name')


class PlayerAdminForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['team'].queryset = Team.objects.none()

        league_id = None

        if self.is_bound:
            league_id = self.data.get('league') or None
        elif self.instance and self.instance.pk:
            league_id = self.instance.league_id
        elif self.initial.get('league'):
            league_id = self.initial.get('league')

        if league_id:
            self.fields['team'].queryset = Team.objects.filter(
                league_id=league_id,
            ).order_by('name')


class TeamPlayerInlineForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = ('name', 'phone', 'male')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['league'].widget = forms.HiddenInput()


class TeamPlayerInlineFormSet(BaseInlineFormSet):
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)

        if self.instance and self.instance.pk:
            form.instance.team = self.instance
            form.instance.league = self.instance.league
            if 'league' in form.fields:
                form.fields['league'].initial = self.instance.league_id

        return form

    def save_new(self, form, commit=True):
        instance = form.save(commit=False)
        instance.team = self.instance
        instance.league = self.instance.league
        if commit:
            instance.save()
            form.save_m2m()
        return instance

    def save_existing(self, form, instance, commit=True):
        updated_instance = form.save(commit=False)
        updated_instance.team = self.instance
        updated_instance.league = self.instance.league
        if commit:
            updated_instance.save()
            form.save_m2m()
        return updated_instance


class TeamPlayerInline(admin.TabularInline):
    model = Player
    form = TeamPlayerInlineForm
    formset = TeamPlayerInlineFormSet
    extra = 1
    fields = ('name', 'phone', 'male', 'league')
    verbose_name = 'Player'
    verbose_name_plural = 'Players'


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    search_fields = ('name',)
    list_display = ('name', 'team_size', 'results_type', 'day_of_week')
    list_filter = ('results_type', 'day_of_week')
    change_form_template = 'admin/core/league/change_form.html'

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_Fpermission(self, request, obj=None):
        return request.user.is_superuser

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/financial-breakdown/',
                self.admin_site.admin_view(self.financial_breakdown_view),
                name='core_league_financial_breakdown',
            ),
        ]
        return custom_urls + urls

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            league = get_object_or_404(League, pk=object_id)
            if league.results_type == League.ResultsType.EIGHT_BALL:
                extra_context['financial_breakdown_url'] = reverse(
                    'admin:core_league_financial_breakdown',
                    args=[league.pk],
                )
                extra_context['show_financial_breakdown'] = True
        return super().changeform_view(
            request,
            object_id=object_id,
            form_url=form_url,
            extra_context=extra_context,
        )

    def financial_breakdown_view(self, request, object_id):
        league = get_object_or_404(League, pk=object_id, results_type=League.ResultsType.EIGHT_BALL)

        teams = league.teams.all().order_by('name')
        team_count = teams.count()
        team_size = league.team_size

        active_season = league.seasons.all().filter(status=Season.Status.ACTIVE).first()
        weeks = active_season.weeks.filter(number__isnull=False).count() if active_season else 0

        signup_fee = league.signup_fee
        fee_per_player = league.fee_per_player
        greens_fee = league.greens_fee
        tournament_target = league.tournament_target

        weekly_collection_per_team = fee_per_player * Decimal(team_size)
        weekly_greens_total_per_team = greens_fee * Decimal(team_size)
        weekly_payout_pool_per_team = (fee_per_player - greens_fee) * Decimal(team_size)

        total_signup_fees = signup_fee * Decimal(team_count)
        total_weekly_collected = weekly_collection_per_team * Decimal(team_count) * Decimal(weeks)
        total_greens_fees = weekly_greens_total_per_team * Decimal(team_count) * Decimal(weeks)
        total_weekly_payout_pool = weekly_payout_pool_per_team * Decimal(team_count) * Decimal(weeks)
        tournament_money = tournament_target * Decimal(team_count)

        total_payout_amount = total_weekly_payout_pool + tournament_money

        standings_data = []
        if active_season:
            from core.views import build_team_standings
            standings_data = build_team_standings(league, active_season)

        payout_rate = Decimal('0')
        if standings_data:
            total_games_won = sum(Decimal(row['games_won']) for row in standings_data)
            if total_games_won > 0:
                payout_rate = total_payout_amount / total_games_won

        standings = []
        for row in standings_data:
            standings.append({
                'team': row['team'],
                'games_won': row['games_won'],
                'payout': Decimal(row['games_won']) * payout_rate,
            })

        awards = [
            {'label': 'Top Male', 'amount': Decimal('100')},
            {'label': 'Top Female', 'amount': Decimal('100')},
            {'label': 'Most Runouts', 'amount': Decimal('20')},
            {'label': 'Most 8 on the Breaks', 'amount': Decimal('20')},
            {'label': 'Most Sweeps', 'amount': Decimal('20')},
        ]

        return render(request, 'admin/core/league/financial_breakdown.html', {
            'title': f'Financial Breakdown: {league.name}',
            'league': league,
            'team_count': team_count,
            'team_size': team_size,
            'weeks': weeks,
            'fee_per_player': fee_per_player,
            'greens_fee': greens_fee,
            'signup_fee': signup_fee,
            'tournament_target': tournament_target,
            'weekly_collection_per_team': weekly_collection_per_team,
            'weekly_greens_total_per_team': weekly_greens_total_per_team,
            'weekly_payout_pool_per_team': weekly_payout_pool_per_team,
            'total_signup_fees': total_signup_fees,
            'total_weekly_collected': total_weekly_collected,
            'total_greens_fees': total_greens_fees,
            'total_weekly_payout_pool': total_weekly_payout_pool,
            'tournament_money': tournament_money,
            'total_payout_amount': total_payout_amount,
            'payout_rate': payout_rate,
            'standings': standings,
            'awards': awards,
        })

@admin.register(LeagueAdminAccess)
class LeagueAdminAccessAdmin(admin.ModelAdmin):
    list_display = ('user', 'league')
    search_fields = ('user__username', 'league__name')


@admin.register(Venue)
class VenueAdmin(LeagueScopedAdminMixin, admin.ModelAdmin):
    search_fields = ('name', 'phone', 'address')
    list_display = (
        'name',
        'league',
        'phone',
        'number_of_tables',
        'min_home_teams',
        'max_home_teams',
    )

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('league',)
        return ()


@admin.register(Team)
class TeamAdmin(LeagueScopedAdminMixin, admin.ModelAdmin):
    form = TeamAdminForm
    inlines = [TeamPlayerInline]
    list_display = ('name', 'league', 'venue', 'captain', 'team_rank')
    search_fields = ('name',)

    class Media:
        js = ('core/js/team_admin.js',)

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('league', LeagueScopedVenueFilter)
        return (LeagueScopedVenueFilter,)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'league-options/',
                self.admin_site.admin_view(self.league_options_view),
                name='core_team_league_options',
            ),
        ]
        return custom_urls + urls

    def league_options_view(self, request):
        league_id = request.GET.get('league_id')
        team_id = request.GET.get('team_id')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            league_id = str(user_league.pk)

        venues = []
        captains = []

        if league_id:
            venues = list(
                Venue.objects.filter(league_id=league_id)
                .order_by('name')
                .values('id', 'name')
            )

            if team_id:
                captains = list(
                    Player.objects.filter(team_id=team_id)
                    .order_by('name')
                    .values('id', 'name')
                )

        return JsonResponse({
            'venues': venues,
            'captains': captains,
        })


@admin.register(Player)
class PlayerAdmin(LeagueScopedAdminMixin, admin.ModelAdmin):
    form = PlayerAdminForm
    list_display = ('name', 'league', 'team', 'phone')
    search_fields = ('name', 'phone')

    class Media:
        js = ('core/js/player_admin.js',)

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('league', LeagueScopedTeamFilter)
        return (LeagueScopedTeamFilter,)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'league-teams/',
                self.admin_site.admin_view(self.league_teams_view),
                name='core_player_league_teams',
            ),
        ]
        return custom_urls + urls

    def league_teams_view(self, request):
        league_id = request.GET.get('league_id')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            league_id = str(user_league.pk)

        teams = []

        if league_id:
            teams = list(
                Team.objects.filter(league_id=league_id)
                .order_by('name')
                .values('id', 'name')
            )

        return JsonResponse({
            'teams': teams,
        })


class LeagueScopedVenueFilter(admin.SimpleListFilter):
    title = 'venue'
    parameter_name = 'venue'

    def lookups(self, request, model_admin):
        queryset = Venue.objects.all().order_by('name')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            queryset = queryset.filter(league=user_league)

        return [(venue.pk, venue.name) for venue in queryset]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(venue_id=self.value())
        return queryset


class LeagueScopedTeamFilter(admin.SimpleListFilter):
    title = 'team'
    parameter_name = 'team'

    def lookups(self, request, model_admin):
        queryset = Team.objects.all().order_by('name')

        user_league = get_user_league(request)
        if not request.user.is_superuser and user_league is not None:
            queryset = queryset.filter(league=user_league)

        return [(team.pk, team.name) for team in queryset]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(team_id=self.value())
        return queryset