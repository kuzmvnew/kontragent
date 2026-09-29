#!/usr/bin/env bash
set -euo pipefail

repository="${1:-$(pwd)}"
output_dir="${2:-${repository}/dist}"

exec uv run --project "$repository" --frozen --no-dev python "$repository/scripts/release_artifact.py" build --source "$repository" --output-dir "$output_dir" --python-minor 3.14 --postgresql-version 18.6
