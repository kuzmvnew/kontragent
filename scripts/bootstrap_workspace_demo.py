#!/usr/bin/env python3
"""Bootstrap or reset the dedicated local NEXT Company Demo databases."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.workspace_demo_support import (  # noqa: E402
    RESET_CONFIRMATION,
    bootstrap_demo,
    demo_password_from_environment,
    redacted_summary,
    require_demo_mode,
    reset_demo,
)


def _database_urls() -> tuple[str, str, str]:
    operational = os.getenv("DATABASE_URL", "")
    public_import = os.getenv("PUBLIC_IMPORT_DATABASE_URL", "")
    public_web = os.getenv("PUBLIC_DATABASE_URL", "")
    return operational, public_import, public_web


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="create or reconcile Demo state")
    bootstrap.add_argument("--profile", choices=("clean", "showcase"), default="clean")
    reset = commands.add_parser("reset", help="remove only recognized local Demo state")
    reset.add_argument("--confirm", required=True, help=f"must equal {RESET_CONFIRMATION}")
    args = parser.parse_args()

    require_demo_mode()
    operational, public_import, public_web = _database_urls()
    if args.command == "reset":
        result = reset_demo(
            operational_url=operational,
            public_import_url=public_import,
            confirmation=args.confirm,
        )
    else:
        password = demo_password_from_environment()
        result = bootstrap_demo(
            operational_url=operational,
            public_import_url=public_import,
            public_web_url=public_web,
            password=password,
            profile=args.profile,
        )
        result["public_url"] = os.getenv("PUBLIC_ORIGIN", "http://127.0.0.1:8080")
        result["workspace_url"] = os.getenv("WORKSPACE_ORIGIN", "http://127.0.0.1:8081")
    print(redacted_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
