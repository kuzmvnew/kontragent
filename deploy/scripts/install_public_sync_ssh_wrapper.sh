#!/usr/bin/env bash
set -euo pipefail

mode="${1:-install}"
[[ "$mode" == "install" || "$mode" == "--check" ]] || {
  echo "usage: install_public_sync_ssh_wrapper.sh [--check]" >&2
  exit 2
}
[[ "$#" -le 1 ]] || {
  echo "usage: install_public_sync_ssh_wrapper.sh [--check]" >&2
  exit 2
}

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source_wrapper="$script_root/scripts/public_sync_ssh_wrapper.py"
destination="/usr/local/sbin/nextcompany-public-sync-ssh"

if [[ "$mode" == "--check" ]]; then
  [[ -f "$destination" ]] && cmp --silent "$source_wrapper" "$destination" || {
    echo "installed public-sync SSH wrapper differs from the repository" >&2
    exit 1
  }
else
  install -o root -g root -m 0755 "$source_wrapper" "$destination"
fi

sha256sum "$destination"
