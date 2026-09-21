#!/usr/bin/env python3
"""Verify the live PostgreSQL schema against every registered ORM model.

No DDL, data writes, create_all or automatic repairs. Exit 1 on incompatibility.
Additional migration-owned indexes and equivalent scalar defaults are reported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401 -- register every model, including snapshots
from app.database.base import Base
from migrations.schema_validation_v1 import audit_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            report = audit_metadata(connection, Base.metadata)
            report.update({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "evidence_class": "VERIFIED_RUNTIME", "database": engine.url.database,
                "alembic_revisions": list(connection.scalars(text("SELECT version_num FROM alembic_version"))) if inspect(connection).has_table("alembic_version") else [],
            })
    finally:
        engine.dispose()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("database", "compatible", "model_table_count", "database_table_count", "missing_tables", "extra_tables", "errors", "observations", "alembic_revisions")}, ensure_ascii=False, indent=2))
    return 0 if report["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
