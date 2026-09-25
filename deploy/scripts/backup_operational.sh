#!/usr/bin/env bash
set -euo pipefail

backup_dir="${OPERATIONS_BACKUP_DIR:-/var/backups/nextcompany-operational}"
database_url="${DATABASE_URL:?DATABASE_URL is required}"
database_url="${database_url/postgresql+psycopg:/postgresql:}"
operational_database="${OPERATIONS_DATABASE_NAME:-${database_url%%\?*}}"
operational_database="${operational_database%/}"
operational_database="${operational_database##*/}"
if [[ ! "$operational_database" =~ ^[a-zA-Z][a-zA-Z0-9_]{2,62}$ ]]; then
  echo "invalid operational database name" >&2
  exit 1
fi
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="${backup_dir}/nextcompany_operational_${timestamp}.dump"

install -d -m 0700 "$backup_dir"
exec 9>"${backup_dir}/.backup.lock"
flock -n 9
if command -v pg_dump >/dev/null 2>&1; then
  pg_dump --format=custom --no-owner --no-acl --file="$backup_file" "$database_url"
elif command -v docker >/dev/null 2>&1 \
  && docker inspect nextcompany-task045-postgres >/dev/null 2>&1; then
  docker exec nextcompany-task045-postgres sh -c \
    'exec pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" "$1"' \
    sh "$operational_database" \
    > "$backup_file"
else
  echo "pg_dump is unavailable" >&2
  exit 1
fi
sha256sum "$backup_file" > "${backup_file}.sha256"

restore_status="NOT CONFIGURED"
restore_failed=0
if [[ -n "${OPERATIONS_RESTORE_TEST_DATABASE_URL:-}" ]] \
  && command -v pg_restore >/dev/null 2>&1 \
  && command -v psql >/dev/null 2>&1; then
  restore_url="${OPERATIONS_RESTORE_TEST_DATABASE_URL/postgresql+psycopg:/postgresql:}"
  if pg_restore --clean --if-exists --no-owner --no-acl --dbname="$restore_url" "$backup_file" \
    && psql "$restore_url" -v ON_ERROR_STOP=1 -Atqc 'SELECT 1' >/dev/null; then
    restore_status="PASS"
  else
    restore_status="FAIL"
    restore_failed=1
  fi
elif [[ -n "${OPERATIONS_RESTORE_TEST_DATABASE:-}" ]] \
  && command -v docker >/dev/null 2>&1 \
  && docker inspect nextcompany-task045-postgres >/dev/null 2>&1; then
  restore_database="${OPERATIONS_RESTORE_TEST_DATABASE}"
  if [[ ! "$restore_database" =~ ^[a-zA-Z][a-zA-Z0-9_]{2,62}$ ]]; then
    echo "invalid restore-test database name" >&2
    exit 1
  fi
  postgres_user="$(docker exec nextcompany-task045-postgres printenv POSTGRES_USER)"
  if [[ "$restore_database" == "$operational_database" ]]; then
    echo "restore-test database must differ from production" >&2
    exit 1
  fi
  docker exec nextcompany-task045-postgres dropdb --if-exists --force --username="$postgres_user" "$restore_database"
  docker exec nextcompany-task045-postgres createdb --username="$postgres_user" "$restore_database"
  if docker exec -i nextcompany-task045-postgres pg_restore \
      --no-owner --no-acl --username="$postgres_user" --dbname="$restore_database" \
      < "$backup_file" \
    && docker exec nextcompany-task045-postgres psql \
      --username="$postgres_user" --dbname="$restore_database" \
      -v ON_ERROR_STOP=1 -Atqc 'SELECT 1' >/dev/null; then
    restore_status="PASS"
  else
    restore_status="FAIL"
    restore_failed=1
  fi
  docker exec nextcompany-task045-postgres dropdb --if-exists --force --username="$postgres_user" "$restore_database"
fi
printf '{"status":"%s","checked_at":"%s"}\n' "$restore_status" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${backup_file}.restore-test.json"
printf '%s\n' "$backup_file"
exit "$restore_failed"
