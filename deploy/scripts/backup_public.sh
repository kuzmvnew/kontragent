#!/usr/bin/env bash
set -euo pipefail

backup_dir="${PUBLIC_BACKUP_DIR:-/var/backups/nextcompany}"
retention_days="${PUBLIC_BACKUP_RETENTION_DAYS:-14}"
database_url="${PUBLIC_IMPORT_DATABASE_URL:?PUBLIC_IMPORT_DATABASE_URL is required}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="${backup_dir}/nextcompany_public_${timestamp}.dump"

install -d -m 0700 "$backup_dir"
exec 9>"${backup_dir}/.backup.lock"
flock -n 9
pg_dump --format=custom --no-owner --no-acl --file="$backup_file" "$database_url"
sha256sum "$backup_file" > "${backup_file}.sha256"
find "$backup_dir" -type f -name 'nextcompany_public_*.dump' -mtime "+${retention_days}" -delete
find "$backup_dir" -type f -name 'nextcompany_public_*.dump.sha256' -mtime "+${retention_days}" -delete
printf '%s\n' "$backup_file"
