#!/usr/bin/env python3
"""Profile the persisted public company read path for representative INNs."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import time
from pathlib import Path

from sqlalchemy import event

import main
from app.aggregators import company_aggregator
from app.contracts.company_views import PublicCompanyView
from app.database.postgres import engine


def percentile(values: list[float], percentile_value: int) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile_value / 100 * len(ordered)) - 1)
    return ordered[index]


def profile(inns: list[str]) -> dict:
    sql_count = 0
    external_calls = 0
    sql_tables: Counter[str] = Counter()

    def count_sql(_conn, _cursor, statement, *_args):
        nonlocal sql_count
        sql_count += 1
        lowered = " ".join(statement.lower().split())
        for token in (" from ", " join ", " update ", " into "):
            start = 0
            while True:
                index = lowered.find(token, start)
                if index < 0:
                    break
                table = lowered[index + len(token):].split(None, 1)[0].strip('"')
                if table and table[0].isalpha():
                    sql_tables[table] += 1
                start = index + len(token)

    def forbid_external(*_args, **_kwargs):
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("public read attempted an external provider call")

    original_fetch = company_aggregator.fetch_external_sources
    company_aggregator.fetch_external_sources = forbid_external
    event.listen(engine, "before_cursor_execute", count_sql)
    rows = []
    try:
        for inn in inns:
            before = sql_count
            started = time.perf_counter()
            company = main.load_company_read_model(inn)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if company is None:
                rows.append({"inn": inn, "status": "not_found", "latency_ms": round(elapsed_ms, 3), "sql_queries": sql_count - before, "payload_bytes": 0})
                continue
            payload = PublicCompanyView.from_read_model(company).model_dump(mode="json")
            size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            rows.append({"inn": inn, "status": "found", "latency_ms": round(elapsed_ms, 3), "sql_queries": sql_count - before, "payload_bytes": size})
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
        company_aggregator.fetch_external_sources = original_fetch

    latencies = [row["latency_ms"] for row in rows]
    query_counts = [row["sql_queries"] for row in rows]
    return {
        "companies": len(rows),
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "query_count_min": min(query_counts),
        "query_count_max": max(query_counts),
        "external_network_calls": external_calls,
        "query_table_counts": dict(sql_tables.most_common()),
        "rows": rows,
    }


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inn", action="append", required=True, dest="inns")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if len(args.inns) < 5:
        raise SystemExit("Provide at least five representative --inn values")
    report = profile(args.inns)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main_cli()
