#!/usr/bin/env bash
set -euo pipefail

release_dir="${1:?usage: run_release_migrations.sh RELEASE_DIR operational|public}"
lane="${2:?migration lane is required}"
[[ "$release_dir" == /* && -d "$release_dir" ]] || {
  echo "release directory must be an existing absolute path" >&2
  exit 2
}
python="$release_dir/.venv/bin/python"
[[ -x "$python" ]] || { echo "release Python is not executable" >&2; exit 2; }

case "$lane" in
  operational)
    : "${DATABASE_URL:?DATABASE_URL is required}"
    config="$release_dir/alembic.ini"
    ;;
  public)
    : "${PUBLIC_IMPORT_DATABASE_URL:?PUBLIC_IMPORT_DATABASE_URL is required}"
    config="$release_dir/public_alembic.ini"
    ;;
  *)
    echo "lane must be operational or public" >&2
    exit 2
    ;;
esac

exec "$python" -m alembic -c "$config" upgrade head
