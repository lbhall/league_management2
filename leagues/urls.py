"""
URL configuration for leagues project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
"""
from django.contrib import admin
from django.urls import path

from core.views import (
    archived_seasons,
    contact_info,
    home,
    one_pocket_full_schedule_modal,
    player_scores_modal,
    player_stats,
    rules,
    schedule,
    standings,
    team_detail,
    team_schedule_modal,
)

urlpatterns = [
    path('', home, name='home'),
    path('schedule/', schedule, name='schedule'),
    path('standings/', standings, name='standings'),
    path('player-stats/', player_stats, name='player_stats'),
    path('archived-seasons/', archived_seasons, name='archived_seasons'),
    path('contact-info/', contact_info, name='contact_info'),
    path('rules/', rules, name='rules'),
    path('teams/<int:team_id>/', team_detail, name='team_detail'),
    path('teams/<int:team_id>/schedule-modal/', team_schedule_modal, name='team_schedule_modal'),
    path('one-pocket/full-schedule-modal/', one_pocket_full_schedule_modal, name='one_pocket_full_schedule_modal'),
    path('players/<int:player_id>/scores-modal/', player_scores_modal, name='player_scores_modal'),
    path('admin/', admin.site.urls),
]