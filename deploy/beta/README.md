# beta.emcfunleague.com — staging site

A separate vhost on the same server running the **same code (main)** but with its
**own database**, so features gated by per-league flags (e.g. `dual_entry_scoring`)
can be enabled and tested without touching production.

**The beta database is always separate.** Settings only use the production DB for
the production checkout itself (`/var/www/emcfunleague.com/source`); every other
checkout — including this beta vhost — uses its own `source/db.sqlite3`. So beta
can't read or migrate production even if a `manage.py` command is run without any
special env. The beta systemd unit and `deploy.beta.sh` also set `DJANGO_DB_PATH`
explicitly as belt-and-suspenders.

## One-time server provisioning (run once, as the deploy user)

```bash
# 1. Create the vhost + venv
sudo mkdir -p /var/www/beta.emcfunleague.com
sudo chown -R bhall:www-data /var/www/beta.emcfunleague.com
cd /var/www/beta.emcfunleague.com
git clone git@github.com:lbhall/league_management2.git source
python3 -m venv venv
venv/bin/pip install -r source/requirements.txt

# 2. Seed the beta database — recommended: copy production (real teams/players/
#    schedule) into beta's OWN separate file, then apply any new migrations and
#    enable dual-entry on the beta copy. This never affects production.
cp /var/www/emcfunleague.com/source/db.sqlite3 source/db.sqlite3
source/venv/bin/python source/manage.py migrate
source/venv/bin/python source/manage.py shell -c \
  "from core.models import League; League.objects.filter(name='EMC Fun Pool League').update(dual_entry_scoring=True)"
#    (Or start empty instead of copying: just run `manage.py migrate`.)

# 3. systemd unit (edit SECRET_KEY first)
sudo cp source/deploy/beta/gunicorn.beta.service /etc/systemd/system/gunicorn.beta.service
sudo systemctl daemon-reload
sudo systemctl enable --now gunicorn.beta

# 4. nginx + TLS
sudo cp source/deploy/beta/beta.nginx.conf /etc/nginx/sites-available/beta.emcfunleague.com
sudo ln -s /etc/nginx/sites-available/beta.emcfunleague.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d beta.emcfunleague.com

# 5. Passwordless sudo for the beta deploy restart
echo 'bhall ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart gunicorn.beta' \
  | sudo tee /etc/sudoers.d/beta-deploy
```

## Deploying

Beta is the **first** deploy target, production is a manual promotion:

- **Beta — automatic:** every merge to `main` runs the CI pipeline
  (`ci.yml`), and its final `Deploy to beta` job runs
  `deploy.beta.sh` over SSH. (You can also run it by hand on the server:
  `bash /var/www/beta.emcfunleague.com/source/deploy.beta.sh`.)
- **Production — manual:** after verifying the change on
  beta.emcfunleague.com, run the **Deploy production** workflow from the
  GitHub Actions tab (`workflow_dispatch`), which runs the prod `deploy.sh`.
  Production no longer auto-deploys on merge.

## Beta-only behaviors

The beta systemd unit sets extra env vars that stay off in production:

- `SCORE_UPCOMING=true` — lets captains/admins enter scores for **upcoming**
  (future-dated) matches, so features can be exercised before match day.

To toggle one of these, edit the `Environment=` line in
`/etc/systemd/system/gunicorn.beta.service`, then
`sudo systemctl daemon-reload && sudo systemctl restart gunicorn.beta`.

## Notes

- Collectstatic/migrate in `deploy.beta.sh` export `DJANGO_DB_PATH` so they only
  ever touch the beta DB.
- Set a real `SECRET_KEY` in the systemd unit before enabling.
- Match the `--bind` (unix socket vs. port) and `User`/`Group` to your existing
  production `gunicorn` unit if they differ.
