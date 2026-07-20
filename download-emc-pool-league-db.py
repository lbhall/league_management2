#!/usr/bin/env python3
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_HOST = "emcfunleague.com"
DEFAULT_USER = "bhall"
DEFAULT_PORT = "56"
REMOTE_DB_PATH = "/var/www/emcfunleague.com/source/db.sqlite3"
DEFAULT_LOCAL_DB = "db.sqlite3"
DEFAULT_BACKUP_DIR = "database_backups"


def run_scp_download(remote_path, local_path, args):
    scp_cmd = [
        "scp", "-P", args.port,
        f"{args.user}@{args.host}:{remote_path}",
        str(local_path),
    ]
    subprocess.run(scp_cmd, check=True)
    if not local_path.exists() or local_path.stat().st_size == 0:
        raise RuntimeError(f"Downloaded file is missing or empty: {local_path}")


def backup_existing_local_db(local_db_path, backup_dir):
    if not local_db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{local_db_path.name}.{timestamp}.back"
    shutil.copy2(local_db_path, backup_path)
    return backup_path


def add_common_arguments(parser):
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"SSH host (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER, help=f"SSH username (default: {DEFAULT_USER})")
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"SSH port (default: {DEFAULT_PORT})")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help=f"Local backup directory (default: {DEFAULT_BACKUP_DIR})")


def cmd_use_local(args):
    project_root = Path(__file__).resolve().parent

    local_db_path = Path(args.local_db).expanduser()
    if not local_db_path.is_absolute():
        local_db_path = project_root / local_db_path

    backup_dir = Path(args.backup_dir).expanduser()
    if not backup_dir.is_absolute():
        backup_dir = project_root / backup_dir

    temp_path = project_root / ".db.sqlite3.download"

    print("Downloading production database...")
    print(f"Remote: {args.user}@{args.host}:{REMOTE_DB_PATH}")
    print(f"Local:  {local_db_path}")

    try:
        run_scp_download(REMOTE_DB_PATH, temp_path, args)

        backup_path = backup_existing_local_db(local_db_path, backup_dir)
        if backup_path:
            print(f"Backed up existing local database to {backup_path}")

        shutil.move(str(temp_path), str(local_db_path))
        print("Successfully replaced local database with production database.")

    except subprocess.CalledProcessError as e:
        print(f"Error: scp failed with exit code {e.returncode}", file=sys.stderr)
        if temp_path.exists():
            temp_path.unlink()
        sys.exit(e.returncode)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if temp_path.exists():
            temp_path.unlink()
        sys.exit(1)


def cmd_backup(args):
    project_root = Path(__file__).resolve().parent

    backup_dir = Path(args.backup_dir).expanduser()
    if not backup_dir.is_absolute():
        backup_dir = project_root / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    local_path = backup_dir / f"{date_str}.db.sqlite3"

    print("Downloading production database backup...")
    print(f"Remote: {args.user}@{args.host}:{REMOTE_DB_PATH}")
    print(f"Backup: {local_path}")

    try:
        run_scp_download(REMOTE_DB_PATH, local_path, args)
        print(f"Successfully saved backup to {local_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error: scp failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Download the production SQLite database."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    use_local_parser = subparsers.add_parser(
        "use-local",
        help="Download production database and replace local db.sqlite3.",
    )
    use_local_parser.add_argument(
        "--local-db",
        default=DEFAULT_LOCAL_DB,
        help=f"Local database path to replace (default: {DEFAULT_LOCAL_DB})",
    )
    add_common_arguments(use_local_parser)

    backup_parser = subparsers.add_parser(
        "backup",
        help="Download production database into the backup directory.",
    )
    add_common_arguments(backup_parser)

    args = parser.parse_args()

    if args.command == "use-local":
        cmd_use_local(args)
    elif args.command == "backup":
        cmd_backup(args)


if __name__ == "__main__":
    main()
