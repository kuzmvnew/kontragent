#!/usr/bin/env python3
"""Operate the non-HTTP local NEXT Company Demo event driver and checks."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.workspace_demo_support import (  # noqa: E402
    advance_demo_event,
    demo_acceptance_truth,
    redacted_summary,
    require_demo_mode,
    validate_demo_topology,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    advance = commands.add_parser("advance-event")
    advance.add_argument("--inn", default="9000000046")
    commands.add_parser("verify")
    args = parser.parse_args()

    require_demo_mode()
    operational = os.getenv("DATABASE_URL", "")
    public_import = os.getenv("PUBLIC_IMPORT_DATABASE_URL", "")
    public_web = os.getenv("PUBLIC_DATABASE_URL", "")
    validate_demo_topology(
        operational_url=operational,
        public_import_url=public_import,
        public_web_url=public_web,
    )
    if args.command == "verify":
        result = demo_acceptance_truth(
            operational_url=operational,
            public_import_url=public_import,
            public_web_url=public_web,
        )
    else:
        engine = sa.create_engine(operational, pool_pre_ping=True)
        DemoSession = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with DemoSession() as session:
                result = advance_demo_event(session, inn=args.inn)
                session.commit()
        finally:
            engine.dispose()
    print(redacted_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
