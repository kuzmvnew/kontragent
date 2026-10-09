#!/usr/bin/env python3
"""Prepare or verify the guarded one-company Alan local real-data preview."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.local_real_preview_support import (
    OWNER_EMAIL,
    RELEASE_ID,
    build_bundle,
    bootstrap_workspace,
    import_public_bundle,
    require_preview_mode,
    validate_database_topology,
    verify_preview,
)


def _urls() -> tuple[str, str, str]:
    source = os.getenv("ALAN_PREVIEW_SOURCE_DATABASE_URL", "")
    operational = os.getenv("DATABASE_URL", "")
    public = os.getenv("PUBLIC_IMPORT_DATABASE_URL") or os.getenv("PUBLIC_DATABASE_URL", "")
    validate_database_topology(
        source_url=source,
        operational_url=operational,
        public_url=public,
    )
    return source, operational, public


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("public_releases/local-real-preview-alan"),
    )
    args = parser.parse_args()
    require_preview_mode(dict(os.environ))
    source_url, operational_url, public_url = _urls()
    if args.command == "prepare":
        password = os.getenv("NEXTCOMPANY_PREVIEW_PASSWORD", "")
        if not password:
            parser.error("NEXTCOMPANY_PREVIEW_PASSWORD is required for prepare")
        bundle_dir, evidence = build_bundle(source_url, args.output_root.resolve())
        public_result = import_public_bundle(public_url, bundle_dir)
        workspace_result = bootstrap_workspace(operational_url, password)
        result = {
            "prepared": True,
            "release_id": RELEASE_ID,
            "bundle": str(bundle_dir),
            "owner_email": OWNER_EMAIL,
            "source_revision": {
                "snapshot_id": evidence["firmoteka"]["snapshot_id"],
                "raw_sha256": evidence["firmoteka"]["raw_sha256"],
                "risk_assessment_id": evidence["risk"]["assessment_id"],
                "summary_id": evidence["summary"]["summary_id"],
                "company_view_revision": evidence["company_view"]["revision"],
            },
            "public_import": public_result,
            "workspace": workspace_result,
        }
    else:
        result = verify_preview(operational_url, public_url)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
