#!/usr/bin/env bash
set -euo pipefail

source_repository="${1:-$(pwd)}"
output_dir="${2:-${source_repository}/dist}"
builder_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

exec uv run --project "$builder_root" --frozen --no-dev python "$builder_root/scripts/release_artifact.py" build --source "$source_repository" --output-dir "$output_dir" --python-minor 3.14 --postgresql-version 18.6
