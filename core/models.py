from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator


# Create your models here.
class League(models.Model):
    class ResultsType(models.TextChoices):
        DARTS = 'darts', 'Darts'
        EIGHT_BALL = '8_ball', '8 Ball League'
        ONE_POCKET = 'one_pocket', 'One Pocket'

    class DayOfWeek(models.TextChoices):
        MONDAY = 'monday', 'Monday'
        TUESDAY = 'tuesday', 'Tuesday'
        WEDNESDAY = 'wednesday', 'Wednesday'
        THURSDAY = 'thursday', 'Thursday'
        FRIDAY = 'friday', 'Friday'
        SATURDAY = 'saturday', 'Saturday'
        SUNDAY = 'sunday', 'Sunday'

    name = models.CharField(max_length=255)
    team_size = models.PositiveIntegerField()
    results_type = models.CharField(
        max_length=20,
        choices=ResultsType.choices,
    )
    day_of_week = models.CharField(
        max_length=10,
        choices=DayOfWeek.choices,
    )
    wide_logo_url = models.URLField(blank=True)

    fee_per_player = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=10,
        help_text='Amount charged per player.',
    )
    greens_fee = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=2,
        help_text='Portion of fee_per_player paid to the venue.',
    )
    tournament_target = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=90,
        help_text='Target amount per team added to the end-of-season tournament.',
    )
    signup_fee = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=30,
        help_text='Signup fee charged per team.',
    )

    def __str__(self):
        return self.name


class LeagueAdminAccess(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='league_admin_access',
    )
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='admin_users',
    )

    def __str__(self):
        return f'{self.user} -> {self.league}'


class Venue(models.Model):
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='venues',
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20)
    address = models.TextField()
    map_coords = models.CharField(max_length=100, blank=True)
    number_of_tables = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )
    max_home_teams = models.PositiveIntegerField()
    min_home_teams = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['league', 'name'],
                name='unique_venue_name_per_league',
            ),
        ]

    def clean(self):
        super().clean()
        if self.max_home_teams < self.min_home_teams:
            raise ValidationError({
                'max_home_teams': 'Max home teams must be greater than or equal to min home teams.',
            })

    def __str__(self):
        return self.name


class Team(models.Model):
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='teams',
    )
    venue = models.ForeignKey(
        Venue,
        on_delete=models.CASCADE,
        related_name='teams',
    )
    captain = models.ForeignKey(
        'Player',
        on_delete=models.SET_NULL,
        related_name='captained_teams',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    seed = models.PositiveIntegerField(blank=True, null=True)
    team_rank = models.PositiveIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['league', 'name'],
                name='unique_team_name_per_league',
            ),
            models.UniqueConstraint(
                fields=['league', 'seed'],
                condition=models.Q(seed__isnull=False),
                name='unique_team_seed_per_league',
            ),
        ]

    def clean(self):
        super().clean()
        if self.venue_id and self.league_id and self.venue.league_id != self.league_id:
            raise ValidationError({
                'venue': 'Selected venue must belong to the same league as the team.',
            })
        if self.captain_id:
            if self.league_id and self.captain.league_id != self.league_id:
                raise ValidationError({
                    'captain': 'Selected captain must belong to the same league as the team.',
                })
            if self.pk and self.captain.team_id not in (None, self.pk):
                raise ValidationError({
                    'captain': 'Selected captain must be unassigned or already belong to this team.',
                })
        if self.team_rank is not None and self.league_id and self.league.results_type != League.ResultsType.ONE_POCKET:
            raise ValidationError({
                'team_rank': 'Team rank can only be set for one pocket leagues.',
            })

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if self.league.results_type == League.ResultsType.ONE_POCKET:
            player = Player.objects.get_or_create(
                league=self.league,
                name=self.name,
                defaults={'male': True},  # You can set this based on your requirements
            )[0]

            if not self.captain_id and player.id != self.captain_id:
                self.captain = player
                self.save(update_fields=['captain'])

        if self.captain_id and self.captain.team_id != self.pk:
            self.captain.team = self
            self.captain.save(update_fields=['team'])


    def __str__(self):
        return self.name


class Player(models.Model):
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='players',
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        related_name='players',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True)
    male = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        if self.team_id and self.league_id and self.team.league_id != self.league_id:
            raise ValidationError({
                'team': 'Selected team must belong to the same league as the player.',
            })

    def __str__(self):
        return self.name