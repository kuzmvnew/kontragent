#!/usr/bin/env bash
set -euo pipefail

artifact="${1:?usage: run_staging_acceptance.sh ARTIFACT PREVIOUS_MANIFEST SHAPE_MANIFEST [EVIDENCE_DIR]}"
previous_manifest="${2:?previous-release manifest is required}"
shape_manifest="${3:?production-shape manifest is required}"
evidence_dir="${4:-/var/lib/nextcompany-staging/evidence/$(date -u +%Y%m%dT%H%M%SZ)}"
script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

: "${STAGING_DB_ADMIN_URL:?STAGING_DB_ADMIN_URL is required}"
: "${STAGING_ACCEPTANCE_CONFIRM:?STAGING_ACCEPTANCE_CONFIRM is required}"

mkdir -p "$evidence_dir"
exec "$script_root/.venv/bin/python" "$script_root/scripts/staging_acceptance.py" --artifact "$artifact" --previous-release-manifest "$previous_manifest" --production-shape-manifest "$shape_manifest" --evidence-dir "$evidence_dir"
