#!/usr/bin/env bash
set -euo pipefail

backup_file=""
confirmation=""
while (($#)); do
  case "$1" in
    --backup) backup_file="${2:?missing backup path}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$confirmation" == "RESTORE_PUBLIC_DB" ]] || { echo "Use --confirm RESTORE_PUBLIC_DB" >&2; exit 2; }
[[ -f "$backup_file" ]] || { echo "Backup not found" >&2; exit 2; }
[[ -f "${backup_file}.sha256" ]] || { echo "Checksum not found" >&2; exit 2; }
(cd "$(dirname "$backup_file")" && sha256sum -c "$(basename "${backup_file}.sha256")")
if [[ -z "${PUBLIC_IMPORT_DATABASE_URL:-}" ]]; then
  set -a
  source /etc/nextcompany/importer.env
  set +a
fi
: "${PUBLIC_IMPORT_DATABASE_URL:?PUBLIC_IMPORT_DATABASE_URL is required}"
case "$PUBLIC_IMPORT_DATABASE_URL" in
  postgresql+psycopg://*) libpq_url="postgresql://${PUBLIC_IMPORT_DATABASE_URL#postgresql+psycopg://}" ;;
  postgresql://*) libpq_url="$PUBLIC_IMPORT_DATABASE_URL" ;;
  *) echo "PUBLIC_IMPORT_DATABASE_URL must be a PostgreSQL URL" >&2; exit 2 ;;
esac

/opt/nextcompany/current/deploy/scripts/backup_public.sh
systemctl stop nextcompany-public.service
restore_status=0
pg_restore --clean --if-exists --no-owner --no-acl --dbname="$libpq_url" "$backup_file" || restore_status=$?
systemctl start nextcompany-public.service
(( restore_status == 0 )) || exit "$restore_status"
curl --fail --silent --show-error http://127.0.0.1:8000/api/ready
