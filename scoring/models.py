from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import League, Player


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
