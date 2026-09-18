#!/usr/bin/env python3
"""Run one bounded region-aware official court attempt for the fixed Golden-40."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.providers.general_court_provider import GeneralCourtRouter
from app.services.general_court_service import get_cached_general_court_check, refresh_general_court_check
from scripts.accept_product_recovery_v3 import select_candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()

    candidates = select_candidates(args.pilot)
    inns = [item["inn"] for item in candidates]
    with get_session() as session:
        companies = {
            company.inn: company
            for company in session.scalars(select(Company).where(Company.inn.in_(inns)))
        }
    if set(companies) != set(inns):
        missing = sorted(set(inns) - set(companies))
        raise RuntimeError(f"Golden companies absent from master registry: {missing}")

    router = GeneralCourtRouter()
    providers = {}
    rows = []
    for card in candidates:
        company = companies[card["inn"]]
        region_code = str(company.region_code or "").zfill(2)
        if region_code not in providers:
            route, provider = router.route(company.region_code)
            providers[region_code] = (route, provider)
        route, provider = providers[region_code]
        result = (
            get_cached_general_court_check(company.inn)
            if args.cached_only else
            refresh_general_court_check(company.inn, provider=provider, force_refresh=True)
        )
        rows.append({
            "inn": company.inn, "name": company.full_name or company.name,
            "region_code": company.region_code, "route": route.__dict__,
            "result": result,
        })

    terminal = Counter(row["result"]["result"] for row in rows)
    errors = Counter(
        row["result"].get("reason") for row in rows
        if row["result"]["result"] == "unavailable"
    )
    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_class": "VERIFIED_RUNTIME", "target": 40,
        "mode": "cached_reread_after_live_run" if args.cached_only else "live_official_queries",
        "actual": len(rows), "terminal_states": terminal,
        "unavailable_reasons": errors, "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps({
        "target": 40, "actual": len(rows), "terminal_states": terminal,
        "unavailable_reasons": errors,
    }, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()
