#!/usr/bin/env bash
set -euo pipefail

ssh_target="${PUBLIC_SSH_TARGET:?Set PUBLIC_SSH_TARGET to the authorized public VPS SSH target}"
output_root="${PUBLIC_RELEASE_OUTPUT_ROOT:-$PWD/public_releases}"
manifest="${PUBLIC_RELEASE_MANIFEST:-docs/releases/public-v1-cohort-40.json}"
source_sha="$(git rev-parse HEAD)"
mkdir -p "$output_root"

export_result="$(python scripts/export_public_release.py --manifest "$manifest" --output-root "$output_root" --source-main-sha "$source_sha")"
release_id="$(python -c 'import json,sys; print(json.load(sys.stdin)["release_id"])' <<<"$export_result")"
bundle_dir="${output_root}/${release_id}"
remote_incoming="/var/lib/nextcompany/incoming/${release_id}"

tar -C "$bundle_dir" -cf - manifest.json companies.jsonl.gz checksums.sha256 | \
  ssh "$ssh_target" "sudo install -d -o nextcompany-importer -g nextcompany -m 0750 '$remote_incoming' && sudo -u nextcompany-importer tar -C '$remote_incoming' -xf -"
ssh "$ssh_target" "sudo -u nextcompany-importer /bin/bash -lc 'set -a; source /etc/nextcompany/importer.env; set +a; /opt/nextcompany/current/.venv/bin/python /opt/nextcompany/current/scripts/import_public_release.py \"$remote_incoming\" --expected-release-id \"$release_id\"'"

sample_inns=(0274101890 7720831611 9102309919)
for inn in "${sample_inns[@]}"; do
  curl --fail --silent --show-error "https://nextcompany.pro/companies/${inn}" >/dev/null
  curl --fail --silent --show-error "https://nextcompany.pro/api/company/${inn}" >/dev/null
done
curl --fail --silent --show-error "https://nextcompany.pro/api/ready" | python -c 'import json,sys; value=json.load(sys.stdin); assert value["status"]=="ready" and value["record_count"]==40'
printf 'release_id=%s record_count=40\n' "$release_id"
