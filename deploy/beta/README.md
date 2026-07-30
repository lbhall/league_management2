# beta.emcfunleague.com — staging site

A separate vhost on the same server running the **same code (main)** but with its
**own database**, so features gated by per-league flags (e.g. `dual_entry_scoring`)
can be enabled and tested without touching production.

Isolation is provided by settings: `DJANGO_DB_PATH` overrides the DB location, and
`ALLOWED_HOSTS` scopes the host. The beta systemd unit and `deploy.beta.sh` set
these, so beta never reads or migrates the production database.

## One-time server provisioning (run once, as the deploy user)

```bash
# 1. Create the vhost + venv
sudo mkdir -p /var/www/beta.emcfunleague.com
sudo chown -R bhall:www-data /var/www/beta.emcfunleague.com
cd /var/www/beta.emcfunleague.com
git clone git@github.com:lbhall/league_management2.git source
python3 -m venv venv
venv/bin/pip install -r source/requirements.txt

# 2. Seed the beta database. Either start fresh:
DJANGO_DB_PATH=/var/www/beta.emcfunleague.com/source/db.sqlite3 \
  source/venv/bin/python source/manage.py migrate
#    ...or copy production and enable dual-entry on the beta copy:
cp /var/www/emcfunleague.com/source/db.sqlite3 source/db.sqlite3
DJANGO_DB_PATH=/var/www/beta.emcfunleague.com/source/db.sqlite3 \
  source/venv/bin/python source/manage.py shell -c \
  "from core.models import League; League.objects.filter(name='EMC Fun Pool League').update(dual_entry_scoring=True)"

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

- **Manually:** run the **Deploy beta** workflow from the GitHub Actions tab
  (`workflow_dispatch`), or on the server: `bash /var/www/beta.emcfunleague.com/source/deploy.beta.sh`.
- **Auto (optional):** once beta is confirmed, add `push: branches: [main]` to
  `.github/workflows/deploy-beta.yml` so every merge to main also deploys beta.

## Notes

- Collectstatic/migrate in `deploy.beta.sh` export `DJANGO_DB_PATH` so they only
  ever touch the beta DB.
- Set a real `SECRET_KEY` in the systemd unit before enabling.
- Match the `--bind` (unix socket vs. port) and `User`/`Group` to your existing
  production `gunicorn` unit if they differ.
