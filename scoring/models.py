from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import League, Player, Team
from scheduling.models import Match


class ScoringProfile(models.Model):
    class Role(models.TextChoices):
        CAPTAIN = 'captain', 'Captain'
        ADMIN = 'admin', 'League Admin'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='scoring_profile',
    )
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name='scoring_profiles',
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        related_name='scoring_profiles',
        null=True,
        blank=True,
        help_text='The player this account belongs to. Required for captains.',
    )
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.CAPTAIN,
    )
    is_approved = models.BooleanField(
        default=False,
        help_text='Accounts must be approved by a league admin before they can enter scores.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if self.role == self.Role.CAPTAIN and not self.player_id:
            raise ValidationError({'player': 'Captain accounts must be linked to a player.'})
        if self.player_id and self.league_id and self.player.league_id != self.league_id:
            raise ValidationError({'player': 'Player must belong to the selected league.'})

    @property
    def team(self):
        return self.player.team if self.player_id else None

    def can_score_match(self, match):
        """Whether this profile may enter scores for the given match."""
        if not self.is_approved:
            return False
        if match.week.season.league_id != self.league_id:
            return False
        if self.role == self.Role.ADMIN:
            return True
        team = self.team
        if team is None:
            return False
        return team.id in (match.home_team_id, match.away_team_id)

    def __str__(self):
        return f'{self.user.username} ({self.get_role_display()})'


class LineupSlot(models.Model):
    """A team's play order for one match (position 1..team_size)."""

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name='scoring_lineup_slots',
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='scoring_lineup_slots',
    )
    position = models.PositiveSmallIntegerField()
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='scoring_lineup_slots',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['match', 'team', 'position'],
                name='unique_lineup_position_per_match_team',
            ),
            models.UniqueConstraint(
                fields=['match', 'team', 'player'],
                name='unique_lineup_player_per_match_team',
            ),
        ]
        ordering = ['team_id', 'position']

    def __str__(self):
        return f'{self.match} — {self.team.name} #{self.position}: {self.player.name}'


class GameResult(models.Model):
    """One game inside a match. In round r, home position i plays the away
    position offset by the round (matching the paper score sheet's rotation:
    round 1 is 1:A 2:B 3:C..., round 2 is 1:B 2:C 3:D..., etc.)."""

    class Winner(models.TextChoices):
        HOME = 'home', 'Home'
        AWAY = 'away', 'Away'

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name='scoring_games',
    )
    round_number = models.PositiveSmallIntegerField()
    home_position = models.PositiveSmallIntegerField()
    winner = models.CharField(max_length=4, choices=Winner.choices)
    runout = models.BooleanField(default=False)
    eight_on_break = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['match', 'round_number', 'home_position'],
                name='unique_game_per_match_round_position',
            ),
        ]
        ordering = ['round_number', 'home_position']

    @staticmethod
    def away_position_for(home_position, round_number, team_size):
        return ((home_position - 1 + round_number - 1) % team_size) + 1

    @property
    def away_position(self):
        team_size = self.match.week.season.league.team_size
        return self.away_position_for(self.home_position, self.round_number, team_size)

    def __str__(self):
        return f'{self.match} R{self.round_number} G{self.home_position} — {self.winner}'
