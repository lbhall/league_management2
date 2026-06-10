import sqlite3
import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import League, Team, Player, Venue
from scheduling.models import Season, Week, Match
from results.models import MatchResult

class Command(BaseCommand):
    help = 'Imports Bogies One Pocket League data from old SQLite database'

    def handle(self, *args, **options):
        db_path = 'database_backups/bogies/20260608-110817.db.sqlite3'
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.OperationalError as e:
            self.stderr.write(f"Could not open database at {db_path}: {e}")
            return
            
        cursor = conn.cursor()

        with transaction.atomic():
            # 1. Get or create League
            # Try to find existing Bogies league
            league = League.objects.filter(name__icontains='Bogies').filter(results_type='one_pocket').first()
            
            if not league:
                league = League.objects.create(
                    name='Bogies One Pocket League',
                    team_size=1,
                    results_type='one_pocket',
                    day_of_week='sunday'
                )
                self.stdout.write(f"Created new league: {league.name}")
            else:
                self.stdout.write(f"Using existing league: {league.name} (ID: {league.id})")

            # 2. Get or create Venue
            venue, _ = Venue.objects.get_or_create(
                league=league,
                name='Bogies',
                defaults={
                    'phone': '', 
                    'address': 'Bogies Billiards', 
                    'number_of_tables': 1, 
                    'max_home_teams': 100, 
                    'min_home_teams': 0
                }
            )

            # 3. Import Players/Teams
            player_map = {}
            cursor.execute("SELECT id, name, phone, skill_level FROM league_player")
            players_data = cursor.fetchall()
            for old_id, name, phone, skill_level in players_data:
                team, t_created = Team.objects.get_or_create(
                    league=league,
                    name=name,
                    defaults={'venue': venue, 'team_rank': skill_level}
                )
                
                # Update team_rank if it's different and team already existed
                if not t_created and team.team_rank != skill_level:
                    team.team_rank = skill_level
                    team.save(update_fields=['team_rank'])

                # Team.save() creates the Player for One Pocket
                player = Player.objects.filter(league=league, team=team, name=name).first()
                if not player:
                    # This shouldn't happen if Team.save() works as expected for one_pocket
                    player = Player.objects.create(league=league, team=team, name=name)
                
                if phone and not player.phone:
                    player.phone = phone
                    player.save(update_fields=['phone'])
                
                player_map[old_id] = team
                if t_created:
                    self.stdout.write(f"Created team/player: {name}")

            # 4. Create Season
            season_name = "Bogies One Pocket - Winter 2026"
            season, s_created = Season.objects.get_or_create(
                league=league,
                name=season_name,
                defaults={'status': 'working'}
            )
            if s_created:
                self.stdout.write(f"Created season: {season.name}")
            else:
                self.stdout.write(f"Using existing season: {season.name}")

            # 5. Import Weeks
            week_map = {}
            cursor.execute("SELECT id, date, number, notes FROM league_week WHERE schedule_id = 199")
            weeks_data = cursor.fetchall()
            for old_id, date_str, number, notes in weeks_data:
                date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                
                # In the new system, week number 0 is not allowed if it's not unique.
                # Usually, number=0 in the old system means a holiday/bye.
                new_number = number if number != 0 else None
                if new_number is None and not (notes or '').strip():
                    notes = "Holiday/Bye"

                week, w_created = Week.objects.get_or_create(
                    season=season,
                    date=date_obj,
                    defaults={'number': new_number, 'notes': notes or ''}
                )
                week_map[old_id] = week
                if w_created:
                    self.stdout.write(f"Created week {new_number} for date {date_obj}")

            # 6. Import Matches
            match_map = {}
            cursor.execute("SELECT id, player1_id, player2_id, week_id, venue FROM league_match")
            matches_data = cursor.fetchall()
            for old_id, p1_id, p2_id, w_id, v_name in matches_data:
                if w_id not in week_map:
                    continue
                
                home_team = player_map.get(p1_id)
                away_team = player_map.get(p2_id)
                
                if not home_team or not away_team:
                    self.stderr.write(f"Could not find teams for match {old_id}: player1={p1_id}, player2={p2_id}")
                    continue
                
                match, m_created = Match.objects.get_or_create(
                    week=week_map[w_id],
                    home_team=home_team,
                    away_team=away_team,
                    defaults={'location': v_name or 'Bogies'}
                )
                match_map[old_id] = match

            # 7. Import MatchResults
            cursor.execute("SELECT match_id, player1_games_won, player2_games_won FROM league_matchscore")
            scores_data = cursor.fetchall()
            for m_id, p1_score, p2_score in scores_data:
                if m_id not in match_map:
                    continue
                
                match = match_map[m_id]
                res, r_created = MatchResult.objects.get_or_create(
                    match=match,
                    defaults={
                        'home_team_score': p1_score,
                        'away_team_score': p2_score
                    }
                )
                if not r_created:
                    res.home_team_score = p1_score
                    res.away_team_score = p2_score
                    res.save(update_fields=['home_team_score', 'away_team_score'])
            
            self.stdout.write(self.style.SUCCESS(f"Import completed! Imported {len(matches_data)} matches and {len(scores_data)} scores."))

        conn.close()
