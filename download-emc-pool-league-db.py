#!/usr/bin/env python3
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Default configurations
DEFAULT_HOST = "emcfunleague.com"
DEFAULT_USER = "bhall"
DEFAULT_PORT = "56"
DEFAULT_REMOTE_PREFIX = "/var/www"
DEFAULT_REMOTE_DB_DIR = "source"
DEFAULT_LOCAL_DB = "db.sqlite3"
DEFAULT_BACKUP_DIR = "database_backups"

# Environment configurations: environment name -> domain mapping
# Environment configurations: environment name -> domain mapping
ENVIRONMENTS = {
    "pool": "emcfunleague.com",
    "bogies": "bogies.emcfunleague.com",
    "coed-darts": "coed.emcfunleague.com",
}

LOCAL_DB_IMPORT_ENVIRONMENTS = {
    "pool": ENVIRONMENTS["pool"],
}

DARTS_IMPORT_ENVIRONMENTS = {
    "coed-darts": ENVIRONMENTS["coed-darts"],
}

ONE_POCKET_IMPORT_ENVIRONMENTS = {
    "bogies": ENVIRONMENTS["bogies"],
}

def get_remote_db_path(args, domain):
    """Build the remote path to the production SQLite database."""
    return f"{args.remote_prefix}/{domain}/{args.remote_db_dir}/db.sqlite3"


def run_scp_download(remote_path, local_path, args):
    """Download a remote file to a local path using scp."""
    scp_cmd = [
        "scp",
        "-P",
        args.port,
        f"{args.user}@{args.host}:{remote_path}",
        str(local_path),
    ]

    subprocess.run(scp_cmd, check=True)

    if not local_path.exists() or local_path.stat().st_size == 0:
        raise RuntimeError(f"Downloaded file is missing or empty: {local_path}")


def backup_existing_local_db(local_db_path, backup_dir):
    """Back up the current local database before replacing it."""
    if not local_db_path.exists():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{local_db_path.name}.{timestamp}.back"

    shutil.copy2(local_db_path, backup_path)
    return backup_path


def download_environment_to_local_db(environment_name, args):
    """Download one environment's production database and use it as the local database."""
    project_root = Path(__file__).resolve().parent
    domain = LOCAL_DB_IMPORT_ENVIRONMENTS[environment_name]

    local_db_path = Path(args.local_db).expanduser()
    if not local_db_path.is_absolute():
        local_db_path = project_root / local_db_path

    backup_dir = Path(args.backup_dir).expanduser()
    if not backup_dir.is_absolute():
        backup_dir = project_root / backup_dir

    if environment_name == "pool":
        remote_path = f"{args.remote_prefix}/{domain}/source/db.sqlite3"
    else:
        remote_path = f"{args.remote_prefix}/{domain}/database/db.sqlite3"
    temp_download_path = project_root / f".{environment_name}.db.sqlite3.download"

    print(f"Downloading '{environment_name}' production database to use as local database...")
    print(f"Remote: {args.user}@{args.host}:{remote_path}")
    print(f"Local:  {local_db_path}")

    try:
        run_scp_download(remote_path, temp_download_path, args)

        backup_path = backup_existing_local_db(local_db_path, backup_dir)
        if backup_path:
            print(f"Backed up existing local database to {backup_path}")

        shutil.move(str(temp_download_path), str(local_db_path))
        print(f"Successfully replaced local database with '{environment_name}' production database.")

    except subprocess.CalledProcessError as e:
        print(f"Error: scp failed with exit code {e.returncode}", file=sys.stderr)

        if temp_download_path.exists():
            temp_download_path.unlink()

        sys.exit(e.returncode)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)

        if temp_download_path.exists():
            temp_download_path.unlink()

        sys.exit(1)


def download_environment_backup(environment_name, domain, date_str, args):
    """Download one environment's production database into the backup directory."""
    project_root = Path(__file__).resolve().parent

    backup_dir = Path(args.backup_dir).expanduser()
    if not backup_dir.is_absolute():
        backup_dir = project_root / backup_dir

    environment_backup_dir = backup_dir / environment_name
    environment_backup_dir.mkdir(parents=True, exist_ok=True)

    remote_path = f"{args.remote_prefix}/{domain}/database/db.sqlite3"
    local_path = environment_backup_dir / f"{date_str}.db.sqlite3"

    print(f"Downloading '{environment_name}' production database backup...")
    print(f"Remote: {args.user}@{args.host}:{remote_path}")
    print(f"Backup: {local_path}")

    run_scp_download(remote_path, local_path, args)
    print(f"Successfully saved '{environment_name}' backup to {local_path}")


def download_all_backups(args):
    """Download all configured production databases into the backup directory."""
    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed_environments = []

    for environment_name, domain in ENVIRONMENTS.items():
        try:
            download_environment_backup(environment_name, domain, date_str, args)
        except subprocess.CalledProcessError as e:
            failed_environments.append(environment_name)
            print(
                f"Error: scp failed for '{environment_name}' with exit code {e.returncode}",
                file=sys.stderr,
            )
        except Exception as e:
            failed_environments.append(environment_name)
            print(f"Error downloading '{environment_name}': {e}", file=sys.stderr)

    if failed_environments:
        print(
            f"Finished with errors. Failed environments: {', '.join(failed_environments)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Successfully downloaded all production database backups.")


def add_common_arguments(parser):
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"SSH host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_USER,
        help=f"SSH username (default: {DEFAULT_USER})",
    )
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        help=f"SSH port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--remote-prefix",
        default=DEFAULT_REMOTE_PREFIX,
        help=f"Remote base directory (default: {DEFAULT_REMOTE_PREFIX})",
    )
    parser.add_argument(
        "--backup-dir",
        default=DEFAULT_BACKUP_DIR,
        help=f"Local backup directory (default: {DEFAULT_BACKUP_DIR})",
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Download production SQLite databases. "
            "Use one environment as the local db, or download all environments as backups."
        )
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    use_local_parser = subparsers.add_parser(
        "use-local",
        help="Download one production database and replace the local db.sqlite3.",
    )
    use_local_parser.add_argument(
        "environment",
        choices=LOCAL_DB_IMPORT_ENVIRONMENTS.keys(),
        help="Production environment to download and use as the local pool league database.",
    )
    use_local_parser.add_argument(
        "--local-db",
        default=DEFAULT_LOCAL_DB,
        help=f"Local database path to replace (default: {DEFAULT_LOCAL_DB})",
    )
    add_common_arguments(use_local_parser)

    backup_all_parser = subparsers.add_parser(
        "backup-all",
        help="Download all production databases into the backup directory.",
    )
    add_common_arguments(backup_all_parser)

    args = parser.parse_args()

    if args.command == "use-local":
        download_environment_to_local_db(args.environment, args)
    elif args.command == "backup-all":
        download_all_backups(args)


if __name__ == "__main__":
    main()
