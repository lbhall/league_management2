import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'leagues.settings')
django.setup()

from core.models import League  # noqa: E402  (django.setup() must run first)

for league in League.objects.all():
    print(f"League: {league.name}")
    print(f"  Fee per player: {league.fee_per_player}")
    print(f"  Greens fee: {league.greens_fee}")
    print(f"  Signup fee: {league.signup_fee}")
    print(f"  Tournament target: {league.tournament_target}")
