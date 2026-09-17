#!/usr/bin/env python3
"""Record PostgreSQL plans for representative Stage 1.6 search queries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.database.postgres import engine


QUERIES = {
    "exact_inn": ("SELECT id FROM companies WHERE inn = :value LIMIT 20", "8602203965"),
    "prefix_inn": ("SELECT id FROM companies WHERE inn LIKE :value LIMIT 20", "860220%"),
    "exact_ogrn": ("SELECT id FROM companies WHERE ogrn = :value LIMIT 20", "1138602008929"),
    "exact_name": ("SELECT id FROM companies WHERE lower(name) = :value LIMIT 20", 'ооо "сургуттранс"'),
    "fuzzy_name": (
        "SELECT id FROM companies WHERE :value <% lower(name) "
        "ORDER BY word_similarity(:value, lower(name)) DESC LIMIT 20",
        "сургуттанс",
    ),
}


def indexes(node: dict) -> list[str]:
    found = []
    if isinstance(node.get("Plan"), dict):
        found.extend(indexes(node["Plan"]))
    if node.get("Index Name"):
        found.append(node["Index Name"])
    for child in node.get("Plans", []):
        found.extend(indexes(child))
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {}
    with engine.connect() as connection:
        for code, (statement, value) in QUERIES.items():
            plan = connection.execute(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}"),
                {"value": value},
            ).scalar_one()[0]
            result[code] = {
                "indexes": indexes(plan),
                "execution_time_ms": plan.get("Execution Time"),
                "plan": plan,
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: {"indexes": value["indexes"], "execution_time_ms": value["execution_time_ms"]} for key, value in result.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
