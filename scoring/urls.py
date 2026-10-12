from django.urls import path

from . import views

app_name = 'scoring'

urlpatterns = [
    path('', views.match_list, name='match_list'),
    path('signup/', views.signup, name='signup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('pending/', views.pending_approval, name='pending'),
    path('match/<int:match_id>/', views.enter_score, name='enter_score'),
    path('match/<int:match_id>/lineup/', views.lineup, name='lineup'),
    path('match/<int:match_id>/games/', views.games, name='games'),
    path('match/<int:match_id>/resolve/', views.resolve, name='resolve'),
    path('match/<int:match_id>/scan/', views.scan_scoresheet, name='scan_scoresheet'),
    path('match/<int:match_id>/scan/<int:scan_id>/', views.scan_review, name='scan_review'),
    path('players/new/', views.add_player, name='add_player'),
    path('manifest.json', views.manifest, name='manifest'),
    path('sw.js', views.service_worker, name='service_worker'),
]
