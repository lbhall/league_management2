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
    completed_at = models.DateTimeField(null=True, blank=True)

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


class BracketMatch(models.Model):
    SIDE_WINNER = 'W'
    SIDE_LOSER = 'L'
    SIDE_FINAL = 'F'
    SIDE_RESET = 'R'
    SIDE_CHOICES = [
        (SIDE_WINNER, 'Winner'),
        (SIDE_LOSER, 'Loser'),
        (SIDE_FINAL, 'Grand Final'),
        (SIDE_RESET, 'Grand Final Reset'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_READY = 'ready'
    STATUS_COMPLETE = 'complete'
    STATUS_SKIPPED = 'skipped'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_READY, 'Ready'),
        (STATUS_COMPLETE, 'Complete'),
        (STATUS_SKIPPED, 'Skipped'),
    ]

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name='bracket_matches',
    )
    bracket_side = models.CharField(max_length=1, choices=SIDE_CHOICES)
    round_number = models.PositiveIntegerField()
    position = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    is_bye = models.BooleanField(default=False)

    team1 = models.ForeignKey(
        TournamentTeam,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    team2 = models.ForeignKey(
        TournamentTeam,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    winner = models.ForeignKey(
        TournamentTeam,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    winner_next = models.ForeignKey(
        'self',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='winner_sources',
    )
    winner_next_slot = models.PositiveSmallIntegerField(null=True, blank=True)
    loser_next = models.ForeignKey(
        'self',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='loser_sources',
    )
    loser_next_slot = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tournament', 'bracket_side', 'round_number', 'position'],
                name='unique_bracket_match_slot',
            ),
        ]
        ordering = ['bracket_side', 'round_number', 'position']

    def __str__(self):
        return f"{self.get_bracket_side_display()} R{self.round_number}.{self.position} ({self.tournament})"
