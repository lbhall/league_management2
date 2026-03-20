from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import League, Team


class Holiday(models.Model):
    date = models.DateField()
    description = models.CharField(max_length=255)

    class Meta:
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(
                fields=['date', 'description'],
                name='unique_holiday_date_description',
            ),
        ]

    def __str__(self):
        return f'{self.date} - {self.description}'


class Season(models.Model):
    class Status(models.TextChoices):
        WORKING = 'working', 'Working'
        ACTIVE = 'active', 'Active'

    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='seasons',
    )
    name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
    )

    class Meta:
        ordering = ['league__name', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['league', 'name'],
                name='unique_season_name_per_league',
            ),
            models.UniqueConstraint(
                fields=['league'],
                condition=Q(status='working'),
                name='unique_working_season_per_league',
            ),
            models.UniqueConstraint(
                fields=['league'],
                condition=Q(status='active'),
                name='unique_active_season_per_league',
            ),
        ]

    def __str__(self):
        return f'{self.league} - {self.name} ({self.get_status_display()})'


class Week(models.Model):
    season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        related_name='weeks',
    )
    date = models.DateField()
    number = models.PositiveIntegerField(blank=True, null=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['season', 'date']
        constraints = [
            models.UniqueConstraint(
                fields=['season', 'date'],
                name='unique_week_date_per_season',
            ),
            models.UniqueConstraint(
                fields=['season', 'number'],
                condition=Q(number__isnull=False),
                name='unique_week_number_per_season',
            ),
        ]

    def clean(self):
        super().clean()
        if self.number is None and not self.notes.strip():
            raise ValidationError({
                'notes': 'Notes are required when number is not supplied.',
            })

    def __str__(self):
        if self.number is not None:
            return f'{self.season} - Week {self.number}'
        return f'{self.season} - {self.date}'

    def is_holiday(self):
        return self.number is None and bool((self.notes or '').strip())


class Match(models.Model):
    week = models.ForeignKey(
        Week,
        on_delete=models.CASCADE,
        related_name='matches',
    )
    home_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='home_matches',
    )
    away_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='away_matches',
    )
    location = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['week', 'sort_order', 'home_team__name', 'away_team__name']

    def clean(self):
        super().clean()

        if self.home_team_id and self.away_team_id and self.home_team_id == self.away_team_id:
            raise ValidationError({
                'away_team': 'Away team must be different from home team.',
            })

        if self.week_id:
            season_league_id = self.week.season.league_id

            if self.home_team_id and self.home_team.league_id != season_league_id:
                raise ValidationError({
                    'home_team': 'Home team must belong to the same league as the week season.',
                })

            if self.away_team_id and self.away_team.league_id != season_league_id:
                raise ValidationError({
                    'away_team': 'Away team must belong to the same league as the week season.',
                })

    def save(self, *args, **kwargs):
        if not self.location and self.home_team_id and self.home_team.venue_id:
            self.location = self.home_team.venue.name
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.home_team} vs {self.away_team}'


class ArchivedSeason(models.Model):
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='archived_seasons',
    )
    name = models.CharField(max_length=255)
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-archived_at', '-id']

    def __str__(self):
        return f'{self.league} - {self.name}'


class ArchivedTeam(models.Model):
    archived_season = models.ForeignKey(
        ArchivedSeason,
        on_delete=models.CASCADE,
        related_name='teams',
    )
    team_name = models.CharField(max_length=255)
    matches_won = models.PositiveIntegerField(default=0)
    matches_lost = models.PositiveIntegerField(default=0)
    games_won = models.PositiveIntegerField(default=0)
    games_lost = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['team_name']

    def __str__(self):
        return f'{self.archived_season.name} - {self.team_name}'


class ArchivedPlayer(models.Model):
    archived_season = models.ForeignKey(
        ArchivedSeason,
        on_delete=models.CASCADE,
        related_name='players',
    )
    player_name = models.CharField(max_length=255)
    team_name = models.CharField(max_length=255, blank=True)
    games_won = models.PositiveIntegerField(default=0)
    games_lost = models.PositiveIntegerField(default=0)
    run_outs = models.PositiveIntegerField(default=0)
    eight_on_the_breaks = models.PositiveIntegerField(default=0)
    sweeps = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['player_name']

    def __str__(self):
        return f'{self.archived_season.name} - {self.player_name}'
