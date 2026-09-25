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
shared_root="${app_root}/shared"
uv_python_install_dir="${shared_root}/python"
uv_cache_dir="${shared_root}/uv-cache"
uv_home="${shared_root}/uv-home"
uv_config_home="${shared_root}/uv-config"
public_env_file="${NEXTCOMPANY_PUBLIC_ENV_FILE:-/etc/nextcompany/public.env}"
importer_env_file="${NEXTCOMPANY_IMPORTER_ENV_FILE:-/etc/nextcompany/importer.env}"
startup_timeout_seconds="${NEXTCOMPANY_STARTUP_TIMEOUT_SECONDS:-45}"
startup_poll_seconds="${NEXTCOMPANY_STARTUP_POLL_SECONDS:-1}"
startup_curl_timeout_seconds="${NEXTCOMPANY_STARTUP_CURL_TIMEOUT_SECONDS:-3}"
for numeric_setting in \
  "$startup_timeout_seconds" \
  "$startup_poll_seconds" \
  "$startup_curl_timeout_seconds"; do
  [[ "$numeric_setting" =~ ^[1-9][0-9]*$ ]] || {
    echo "Startup health timing values must be positive integers" >&2
    exit 2
  }
done
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

wait_for_public_health() {
  local deadline=$((SECONDS + startup_timeout_seconds))
  local http_status=""
  local remaining_seconds=0
  local request_timeout_seconds=0

  while ((SECONDS < deadline)); do
    if systemctl is-failed --quiet nextcompany-public.service; then
      echo "nextcompany-public.service entered failed state during startup" >&2
      return 1
    fi

    remaining_seconds=$((deadline - SECONDS))
    request_timeout_seconds="$startup_curl_timeout_seconds"
    if ((request_timeout_seconds > remaining_seconds)); then
      request_timeout_seconds="$remaining_seconds"
    fi
    http_status="$(
      curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        --connect-timeout "$request_timeout_seconds" \
        --max-time "$request_timeout_seconds" \
        http://127.0.0.1:8000/api/health 2>/dev/null || true
    )"
    if [[ "$http_status" == "200" ]]; then
      return 0
    fi

    if systemctl is-failed --quiet nextcompany-public.service; then
      echo "nextcompany-public.service entered failed state during startup" >&2
      return 1
    fi
    ((SECONDS < deadline)) || break
    remaining_seconds=$((deadline - SECONDS))
    if ((startup_poll_seconds < remaining_seconds)); then
      sleep "$startup_poll_seconds"
    else
      sleep "$remaining_seconds"
    fi
  done

  if [[ -n "$http_status" && "$http_status" != "000" ]]; then
    echo "Public health check timed out after ${startup_timeout_seconds}s (last HTTP status: $http_status)" >&2
  else
    echo "Public health check timed out after ${startup_timeout_seconds}s without an HTTP response" >&2
  fi
  return 1
}

validate_env_dsn "$public_env_file" PUBLIC_DATABASE_URL
validate_env_dsn "$importer_env_file" PUBLIC_IMPORT_DATABASE_URL
install -d -o root -g nextcompany -m 0750 \
  "$app_root" \
  "$releases_root" \
  "$shared_root" \
  "$uv_python_install_dir" \
  "$uv_cache_dir" \
  "$uv_home" \
  "$uv_config_home"
previous_target=""
capture_previous_target

if [[ -n "$previous_target" && -x "$previous_target/deploy/scripts/backup_public.sh" ]]; then
  systemctl start nextcompany-backup.service
fi
install -d -o root -g nextcompany -m 0750 "$release_dir"
rsync -a --delete \
  --exclude .git \
  --exclude .env \
  --exclude .venv \
  --exclude '__pycache__' \
  "$source_dir/" "$release_dir/"
HOME="$uv_home" \
XDG_CACHE_HOME="$uv_cache_dir" \
XDG_CONFIG_HOME="$uv_config_home" \
UV_CACHE_DIR="$uv_cache_dir" \
UV_PYTHON_INSTALL_DIR="$uv_python_install_dir" \
  uv sync --project "$release_dir" --frozen --no-dev
chown -R root:nextcompany "$uv_python_install_dir"
chmod -R g+rX,g-w,o-rwx "$uv_python_install_dir"
chown -R root:nextcompany "$release_dir"
chmod -R g-w "$release_dir"

release_python="$release_dir/.venv/bin/python"
python_link_target="$(readlink -- "$release_python" 2>/dev/null || true)"
case "$python_link_target" in
  /root|/root/*|/home/root|/home/root/*)
    echo "Release Python must not reference root's home: $python_link_target" >&2
    exit 2
    ;;
esac
[[ -x "$release_python" ]] || {
  echo "Release Python is not executable: $release_python" >&2
  exit 2
}
resolved_release_python="$(readlink -f -- "$release_python" 2>/dev/null || true)"
[[ -n "$resolved_release_python" ]] || {
  echo "Release Python cannot be resolved: $release_python" >&2
  exit 2
}
case "$resolved_release_python" in
  /root|/root/*|/home/root|/home/root/*)
    echo "Release Python must not resolve inside root's home: $resolved_release_python" >&2
    exit 2
    ;;
esac
sudo -u nextcompany-web "$release_python" -c 'import sys; assert sys.executable'

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
if ! wait_for_public_health; then
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
