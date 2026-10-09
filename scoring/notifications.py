"""Email notifications for the scoring app.

Everything routes through Django's email framework, so the delivery backend is
purely a config/ops choice (console by default; point EMAIL_* at local Postfix
or a relay to actually send). Sends fail silently so a mail hiccup never breaks
a signup or a cron run.
"""
from django.conf import settings
from django.core.mail import send_mail

from .models import ScoringProfile


def _base_url():
    return settings.SITE_BASE_URL.rstrip('/')


def notify_new_signup(request, profile):
    """Alert the operator that a new captain has signed up and needs approval.
    No-op unless LEAGUE_NOTIFY_EMAIL is configured."""
    to = getattr(settings, 'LEAGUE_NOTIFY_EMAIL', '')
    if not to:
        return
    player = profile.player
    team = player.team.name if (player and player.team) else '(sub / no team)'
    subject = f'New captain signup: {player.name} — {profile.league.name}'
    body = (
        f'{player.name} signed up to score for {profile.league.name} and is '
        f'waiting for approval.\n\n'
        f'Email: {profile.user.email}\n'
        f'Team: {team}\n\n'
        f'Approve them here:\n{_base_url()}/admin/scoring/scoringprofile/\n'
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to], fail_silently=True)


def find_captains_needing_reminders(days=7, league=None):
    """Map each approved captain -> their unentered matches within the last
    `days`, across active seasons (optionally one league)."""
    from datetime import timedelta

    from django.utils import timezone

    from scheduling.models import Match, Season

    from .views import _match_fully_scored

    today = timezone.localdate()
    cutoff = today - timedelta(days=days)

    seasons = Season.objects.filter(status=Season.Status.ACTIVE)
    if league is not None:
        seasons = seasons.filter(league=league)

    matches = Match.objects.filter(
        week__season__in=seasons,
        week__number__isnull=False,
        week__date__lte=today,
        week__date__gte=cutoff,
    ).select_related('home_team', 'away_team', 'week')

    reminders = {}
    for match in matches:
        if _match_fully_scored(match):
            continue
        captains = ScoringProfile.objects.filter(
            role=ScoringProfile.Role.CAPTAIN,
            is_approved=True,
            player__team_id__in=[match.home_team_id, match.away_team_id],
        ).select_related('user')
        for captain in captains:
            if captain.user.email:
                reminders.setdefault(captain.user, []).append(match)
    return reminders


def send_score_reminders(days=7, league=None):
    """Email each captain a nudge listing their unentered matches. Returns the
    number of emails sent."""
    reminders = find_captains_needing_reminders(days=days, league=league)
    base = _base_url()
    sent = 0
    for user, matches in reminders.items():
        lines = '\n'.join(
            f'  Week {m.week.number} ({m.week.date}): '
            f'{m.home_team.name} vs {m.away_team.name}'
            for m in matches
        )
        subject = 'Reminder: your match score still needs to be entered'
        body = (
            'These matches are still waiting on a score from you:\n\n'
            f'{lines}\n\n'
            f'Enter it in the scoring app:\n{base}/score/\n'
        )
        send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, [user.email],
            fail_silently=True,
        )
        sent += 1
    return sent
