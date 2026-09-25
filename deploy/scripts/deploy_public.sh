#!/usr/bin/env bash
set -euo pipefail

source_dir="${1:?usage: deploy_public.sh SOURCE_DIR}"
[[ -f "$source_dir/pyproject.toml" && -f "$source_dir/public_alembic.ini" ]] || {
  echo "Invalid release source" >&2
  exit 2
}

app_root="${NEXTCOMPANY_APP_ROOT:-/opt/nextcompany}"
[[ "$app_root" == /* ]] || {
  echo "NEXTCOMPANY_APP_ROOT must be absolute" >&2
  exit 2
}
releases_root="${app_root}/releases"
current_path="${app_root}/current"
public_env_file="${NEXTCOMPANY_PUBLIC_ENV_FILE:-/etc/nextcompany/public.env}"
importer_env_file="${NEXTCOMPANY_IMPORTER_ENV_FILE:-/etc/nextcompany/importer.env}"
release_id="$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)"
[[ "$release_id" =~ ^[0-9A-Za-z._-]+$ ]] || {
  echo "Invalid release identifier" >&2
  exit 2
}
release_dir="${releases_root}/${release_id}"
pending_link="${app_root}/.current-${release_id}-$$"
validated_target=""
validation_error=""

cleanup_pending_link() {
  rm -f -- "$pending_link"
}
trap cleanup_pending_link EXIT

validate_env_dsn() {
  local env_file="$1"
  local variable_name="$2"
  [[ -r "$env_file" ]] || {
    echo "Required environment file is not readable: $env_file" >&2
    return 2
  }
  /bin/bash -euo pipefail -c '
    source "$1"
    value="${!2:-}"
    case "$value" in
      postgresql+psycopg://*) ;;
      *) echo "$2 must use postgresql+psycopg://" >&2; exit 2 ;;
    esac
  ' _ "$env_file" "$variable_name"
}

validate_release_target() {
  local candidate="$1"
  local label="$2"
  local resolved=""
  local resolved_releases=""

  validated_target=""
  validation_error=""
  if [[ "$candidate" != /* ]]; then
    validation_error="$label must be absolute"
    return 1
  fi
  if [[ "$candidate" == "$current_path" ]]; then
    validation_error="$label must not equal $current_path"
    return 1
  fi
  if [[ ! -d "$candidate" ]]; then
    validation_error="$label does not exist as a directory"
    return 1
  fi
  resolved="$(readlink -f -- "$candidate" 2>/dev/null || true)"
  resolved_releases="$(readlink -f -- "$releases_root" 2>/dev/null || true)"
  if [[ -z "$resolved" || -z "$resolved_releases" ]]; then
    validation_error="$label cannot be resolved"
    return 1
  fi
  if [[ "$resolved" == "$current_path" ]]; then
    validation_error="$label resolves back to current"
    return 1
  fi
  if [[ "$candidate" != "$resolved" ]]; then
    validation_error="$label must name a concrete release directory directly"
    return 1
  fi
  if [[ "$(dirname -- "$resolved")" != "$resolved_releases" ]]; then
    validation_error="$label must live directly under $releases_root"
    return 1
  fi
  validated_target="$resolved"
}

capture_previous_target() {
  local raw_target=""
  local resolved_current=""

  previous_target=""
  if [[ ! -e "$current_path" && ! -L "$current_path" ]]; then
    return 0
  fi
  if [[ ! -L "$current_path" ]]; then
    echo "$current_path must be a symlink to a release directory" >&2
    return 2
  fi
  raw_target="$(readlink -- "$current_path")"
  if [[ "$raw_target" == "$current_path" ]]; then
    echo "$current_path must not reference itself" >&2
    return 2
  fi
  resolved_current="$(readlink -f -- "$current_path" 2>/dev/null || true)"
  if [[ -z "$resolved_current" ]]; then
    echo "$current_path is broken or self-referencing" >&2
    return 2
  fi
  if ! validate_release_target "$raw_target" "previous release target"; then
    echo "Invalid current release: $validation_error" >&2
    return 2
  fi
  if [[ "$validated_target" != "$resolved_current" ]]; then
    echo "Current symlink does not resolve directly to its release target" >&2
    return 2
  fi
  previous_target="$validated_target"
}

atomic_switch_current() {
  local target="$1"
  local label="$2"

  if ! validate_release_target "$target" "$label"; then
    return 1
  fi
  [[ ! -e "$pending_link" && ! -L "$pending_link" ]] || {
    validation_error="temporary activation link already exists"
    return 1
  }
  ln -s -- "$validated_target" "$pending_link"
  mv -Tf -- "$pending_link" "$current_path"
  [[ "$(readlink -f -- "$current_path" 2>/dev/null || true)" == "$validated_target" ]] || {
    validation_error="atomic symlink switch verification failed"
    return 1
  }
}

deactivate_failed_release() {
  local active_target=""
  active_target="$(readlink -f -- "$current_path" 2>/dev/null || true)"
  if [[ "$active_target" == "$release_dir" ]]; then
    rm -f -- "$current_path"
  fi
}

validate_env_dsn "$public_env_file" PUBLIC_DATABASE_URL
validate_env_dsn "$importer_env_file" PUBLIC_IMPORT_DATABASE_URL
install -d -o root -g nextcompany -m 0750 "$app_root" "$releases_root"
previous_target=""
capture_previous_target

if [[ -n "$previous_target" && -x "$previous_target/deploy/scripts/backup_public.sh" ]]; then
  systemctl start nextcompany-backup.service
fi
install -d -o root -g nextcompany -m 0750 "$release_dir"
rsync -a --delete --exclude .git --exclude .env --exclude '__pycache__' "$source_dir/" "$release_dir/"
uv sync --project "$release_dir" --frozen --no-dev
chown -R root:nextcompany "$release_dir"
chmod -R g-w "$release_dir"

sudo -u nextcompany-importer /bin/bash -c '
  set -euo pipefail
  set -a
  source "$1"
  set +a
  exec "$2" -c "$3" upgrade head
' _ "$importer_env_file" "$release_dir/.venv/bin/alembic" "$release_dir/public_alembic.ini"

if ! atomic_switch_current "$release_dir" "new release target"; then
  echo "Failed to activate new release: $validation_error" >&2
  exit 2
fi
systemctl daemon-reload
systemctl restart nextcompany-public.service
if ! curl --fail --silent --show-error --retry 8 --retry-delay 1 http://127.0.0.1:8000/api/health >/dev/null; then
  if [[ -n "$previous_target" ]] && validate_release_target "$previous_target" "previous release target"; then
    if atomic_switch_current "$validated_target" "previous release target"; then
      systemctl restart nextcompany-public.service
      echo "Health check failed; rolled back to $previous_target" >&2
      exit 1
    fi
  fi
  rollback_error="${validation_error:-no validated previous release is available}"
  deactivate_failed_release
  systemctl restart nextcompany-public.service
  echo "Health check failed; rollback target invalid: $rollback_error; failed release left inactive" >&2
  exit 1
fi
nginx -t
systemctl reload nginx
printf 'deployed_sha=%s\n' "$release_id"
