from django.db import models
from core.models import Player
from scheduling.models import Season

class Tournament(models.Model):
    season = models.OneToOneField(
        Season,
        on_delete=models.CASCADE,
        related_name='tournament',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Tournament for {self.season}"

class TournamentPlayer(models.Model):
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name='players',
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='tournament_entries',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tournament', 'player'],
                name='unique_player_per_tournament',
            ),
        ]

    def __str__(self):
        return f"{self.player} in {self.tournament}"


class TournamentTeam(models.Model):
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name='tournament_teams',
    )
    a_player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='tournament_teams_as_a',
    )
    b_player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='tournament_teams_as_b',
    )
    team_number = models.PositiveIntegerField()

    class Meta:
        ordering = ['team_number']

    def __str__(self):
        return f"Team {self.team_number}: {self.a_player} & {self.b_player}"
