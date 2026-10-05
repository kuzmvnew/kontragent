#!/usr/bin/env python3
"""Run one explicit local Monitoring P0 scan for an existing subscription.

This is a system detector entry point, not a customer subscription API or a
production scheduler.  Workspace subscription writes remain authorized by the
Workspace service.
"""

from __future__ import annotations

import argparse
import json
import os

import sqlalchemy as sa

from app.database.postgres import SessionLocal
from app.models.company import Company
from public_app.contracts import valid_legal_inn
from workspace_app.monitoring_service import monitor_company_once


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inn", required=True, help="Exact legal-company INN")
    args = parser.parse_args()
    if os.getenv("WORKSPACE_ENV", "local").strip().lower() not in {
        "local",
        "development",
        "test",
    }:
        parser.error("manual Monitoring P0 scan is local/development/test only")
    if not valid_legal_inn(args.inn):
        parser.error("a valid legal-company INN is required")
    with SessionLocal() as session:
        company_id = session.scalar(
            sa.select(Company.id).where(
                Company.inn == args.inn,
                Company.entity_type == "legal",
            )
        )
        if company_id is None:
            parser.error("company is not resolved in the master model")
        result = monitor_company_once(session, company_id=company_id)
        session.commit()
    print(
        json.dumps(
            {
                "company_id": result.company_id,
                "subscription_count": result.subscription_count,
                "detected_change_count": result.detected_change_count,
                "canonical_event_count": result.canonical_event_count,
                "feed_entry_count": result.feed_entry_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
