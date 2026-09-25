#!/usr/bin/env bash
set -euo pipefail

target_sha="${1:?usage: rollback_public_app.sh EXACT_RELEASE_SHA}"
target="/opt/nextcompany/releases/${target_sha}"
[[ -d "$target" ]] || { echo "Exact release not found" >&2; exit 2; }
ln -s "$target" "/opt/nextcompany/.rollback-${target_sha}"
mv -Tf "/opt/nextcompany/.rollback-${target_sha}" /opt/nextcompany/current
systemctl restart nextcompany-public.service
curl --fail --silent --show-error --retry 8 http://127.0.0.1:8000/api/health
