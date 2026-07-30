#!/usr/bin/env bash
set -euo pipefail

# Beta / staging deploy for beta.emcfunleague.com. Runs on the shared server
# out of its own vhost, venv and database — never the production ones.
SOURCE_DIR="/var/www/beta.emcfunleague.com/source"
VENV="/var/www/beta.emcfunleague.com/venv/bin"

# CRITICAL: point management commands at the beta DB. Without this, settings
# auto-detect the production database (which exists on this box) and migrate
# would run against production.
export DJANGO_DB_PATH="$SOURCE_DIR/db.sqlite3"
export ALLOWED_HOSTS="beta.emcfunleague.com"
export DEBUG="false"

echo "==> Pulling latest code..."
git -C "$SOURCE_DIR" pull

echo "==> Installing dependencies..."
"$VENV/pip" install -r "$SOURCE_DIR/requirements.txt" --quiet

echo "==> Running migrations (beta DB)..."
"$VENV/python" "$SOURCE_DIR/manage.py" migrate --noinput

echo "==> Collecting static files..."
"$VENV/python" "$SOURCE_DIR/manage.py" collectstatic --noinput

echo "==> Restarting gunicorn..."
sudo systemctl restart gunicorn.beta

echo "==> Done."
