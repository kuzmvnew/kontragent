#!/usr/bin/env bash
set -euo pipefail

source_dir="${1:?usage: deploy_public.sh SOURCE_DIR}"
[[ -f "$source_dir/pyproject.toml" && -f "$source_dir/public_alembic.ini" ]] || { echo "Invalid release source" >&2; exit 2; }
app_root=/opt/nextcompany
release_id="$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)"
release_dir="${app_root}/releases/${release_id}"
previous_target="$(readlink -f "${app_root}/current" 2>/dev/null || true)"

install -d -o root -g nextcompany -m 0750 "$app_root" "${app_root}/releases"
if [[ -x "${app_root}/current/deploy/scripts/backup_public.sh" ]]; then
  systemctl start nextcompany-backup.service
fi
install -d -o root -g nextcompany -m 0750 "$release_dir"
rsync -a --delete --exclude .git --exclude .env --exclude '__pycache__' "$source_dir/" "$release_dir/"
uv sync --project "$release_dir" --frozen --no-dev
chown -R root:nextcompany "$release_dir"
chmod -R g-w "$release_dir"

sudo -u nextcompany-importer /bin/bash -lc "set -a; source /etc/nextcompany/importer.env; set +a; '$release_dir/.venv/bin/alembic' -c '$release_dir/public_alembic.ini' upgrade head"

ln -s "$release_dir" "${app_root}/.current-${release_id}"
mv -Tf "${app_root}/.current-${release_id}" "${app_root}/current"
systemctl daemon-reload
systemctl restart nextcompany-public.service
if ! curl --fail --silent --show-error --retry 8 --retry-delay 1 http://127.0.0.1:8000/api/health >/dev/null; then
  if [[ -n "$previous_target" ]]; then ln -sfn "$previous_target" "${app_root}/current"; fi
  systemctl restart nextcompany-public.service
  exit 1
fi
nginx -t
systemctl reload nginx
printf 'deployed_sha=%s\n' "$release_id"
