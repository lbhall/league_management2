from django.core.exceptions import ValidationError
from django.db import models

from core.models import Player, Team
from scheduling.models import Match


class MatchResult(models.Model):
    match = models.OneToOneField(
        Match,
        on_delete=models.CASCADE,
        related_name='result',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    home_team_score = models.PositiveIntegerField(null=True, blank=True)
    away_team_score = models.PositiveIntegerField(null=True, blank=True)

    def clean(self):
        super().clean()
        if self.match.week.season.league.results_type != '8_ball':
            raise ValidationError('MatchResult entry is currently only supported for 8-ball leagues.')

    def __str__(self):
        return f"{self.match.home_team} vs {self.match.away_team}"


class PlayerMatchResult(models.Model):
    match_result = models.ForeignKey(
        MatchResult,
        on_delete=models.CASCADE,
        related_name='player_results',
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='match_results',
    )
    represented_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='player_match_results',
    )
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    runouts = models.PositiveIntegerField(default=0)
    eight_on_the_breaks = models.PositiveIntegerField(default=0)
    won_all_games = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['match_result', 'player'],
                name='unique_player_result_per_match',
            ),
        ]
        ordering = ['represented_team__name', 'player__name']

    def clean(self):
        super().clean()

        match = self.match_result.match
        league = match.week.season.league
        team_size = league.team_size

        valid_team_ids = {match.home_team_id, match.away_team_id}
        if self.represented_team_id not in valid_team_ids:
            raise ValidationError({
                'represented_team': 'Represented team must be the home or away team for this match.',
            })

        if self.player_id and self.player.league_id != league.id:
            raise ValidationError({
                'player': 'Player must belong to the same league as this match.',
            })

        if self.player_id and self.player.team_id not in (None, match.home_team_id, match.away_team_id):
            raise ValidationError({
                'player': 'Player must be unassigned or belong to one of the two teams in this match.',
            })

        if self.player_id and self.player.team_id is not None and self.player.team_id != self.represented_team_id:
            raise ValidationError({
                'represented_team': 'Assigned players must score for their own team.',
            })

        if self.wins > team_size:
            raise ValidationError({
                'wins': f'Wins cannot be greater than the team size ({team_size}).',
            })

        expected_losses = team_size - self.wins
        if self.losses != expected_losses:
            raise ValidationError({
                'losses': f'Losses must equal team size - wins ({expected_losses}).',
            })

        if self.won_all_games and self.losses != 0:
            raise ValidationError({
                'won_all_games': 'Won all games can only be true when losses are 0.',
            })

    def save(self, *args, **kwargs):
        league = self.match_result.match.week.season.league
        self.losses = league.team_size - self.wins
        self.won_all_games = self.losses == 0
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.player} - {self.match_result.match}'