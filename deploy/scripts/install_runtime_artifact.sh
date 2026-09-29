#!/usr/bin/env bash
set -euo pipefail

artifact="${1:?usage: install_runtime_artifact.sh ARTIFACT EXPECTED_SHA256 [--activate]}"
expected_sha256="${2:?accepted artifact SHA-256 is required}"
activation="${3:-}"
script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
app_root="${NEXTCOMPANY_APP_ROOT:-/opt/nextcompany}"

[[ "$app_root" == /* ]] || { echo "NEXTCOMPANY_APP_ROOT must be absolute" >&2; exit 2; }
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid expected SHA-256" >&2; exit 2; }
[[ -z "$activation" || "$activation" == "--activate" ]] || {
  echo "third argument must be --activate" >&2
  exit 2
}
[[ -f "$artifact" && -f "${artifact}.sha256" ]] || {
  echo "artifact or checksum sidecar is missing" >&2
  exit 2
}
observed_sha256="$(sha256sum "$artifact" | awk '{print $1}')"
[[ "$observed_sha256" == "$expected_sha256" ]] || {
  echo "accepted artifact SHA-256 mismatch" >&2
  exit 1
}

releases_root="$app_root/releases"
release_dir="$releases_root/$expected_sha256"
pending_dir="$releases_root/.pending-${expected_sha256}-$$"
pending_link="$app_root/.current-${expected_sha256}-$$"
install -d -m 0750 "$app_root" "$releases_root"
trap 'rm -rf -- "$pending_dir"; rm -f -- "$pending_link"' EXIT

if [[ ! -d "$release_dir" ]]; then
  install -d -m 0750 "$pending_dir"
  python3 "$script_root/scripts/release_artifact.py" verify --artifact "$artifact" --extract-to "$pending_dir" --output "$pending_dir/verification.json"
  mv -- "$pending_dir/release" "$release_dir"
fi

python3 "$script_root/scripts/release_artifact.py" verify-tree --release "$release_dir" --output "$releases_root/.verified-${expected_sha256}.json"
status="STAGED"
if [[ "$activation" == "--activate" ]]; then
  ln -s -- "$release_dir" "$pending_link"
  mv -Tf -- "$pending_link" "$app_root/current"
  status="ACTIVATED"
fi
printf '{"status":"%s","artifact_sha256":"%s","release":"%s"}\n' "$status" "$expected_sha256" "$release_dir"
