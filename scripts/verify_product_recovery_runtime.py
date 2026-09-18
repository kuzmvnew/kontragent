#!/usr/bin/env python3
"""Public-read, search, and auth smoke with network/write guards."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event

import main
from app.aggregators import company_aggregator
from app.database.postgres import engine


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inn", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = TestClient(main.app)
    writes = []
    external_calls = []
    log_messages = []

    def observe(_conn, _cursor, statement, *_args):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(verb)

    def forbid_external(*_args, **_kwargs):
        external_calls.append("fetch_external_sources")
        raise AssertionError("public read attempted an external provider call")

    class Capture(logging.Handler):
        def emit(self, record):
            log_messages.append(self.format(record))

    token = "product-recovery-runtime-token"
    previous_token = os.environ.get("KONTRAGENT_INTERNAL_TOKEN")
    original_fetch = company_aggregator.fetch_external_sources
    handler = Capture()
    root_logger = logging.getLogger()
    company_aggregator.fetch_external_sources = forbid_external
    event.listen(engine, "before_cursor_execute", observe)
    root_logger.addHandler(handler)
    os.environ["KONTRAGENT_INTERNAL_TOKEN"] = token
    try:
        statuses = {
            "company_html": client.get(f"/company/{args.inn}").status_code,
            "company_api": client.get(f"/api/company/{args.inn}").status_code,
            "unknown_html": client.get("/company/0000000000").status_code,
            "unknown_api": client.get("/api/company/0000000000").status_code,
        }
        search = client.get("/api/search", params={"q": args.inn})
        statuses["search"] = search.status_code
        statuses["anonymous_mutation"] = client.post(
            f"/api/company/{args.inn}/check", params={"mode": "QUICK"},
        ).status_code
        statuses["anonymous_internal"] = client.get("/internal/api/data-readiness").status_code
        statuses["authorized_internal"] = client.get(
            "/internal/api/data-readiness",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
    finally:
        if previous_token is None:
            os.environ.pop("KONTRAGENT_INTERNAL_TOKEN", None)
        else:
            os.environ["KONTRAGENT_INTERNAL_TOKEN"] = previous_token
        root_logger.removeHandler(handler)
        event.remove(engine, "before_cursor_execute", observe)
        company_aggregator.fetch_external_sources = original_fetch

    assert statuses == {
        "company_html": 200, "company_api": 200,
        "unknown_html": 404, "unknown_api": 404, "search": 200,
        "anonymous_mutation": 401, "anonymous_internal": 401,
        "authorized_internal": 200,
    }
    body = search.json()
    assert any(row.get("inn") == args.inn for row in body.get("results", []))
    assert not writes
    assert not external_calls
    assert all(token not in message for message in log_messages)
    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_class": "VERIFIED_RUNTIME", "statuses": statuses,
        "search_count": body["count"], "search_exact_inn_present": True,
        "database_writes": len(writes), "external_network_calls": len(external_calls),
        "secret_logged": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False))


if __name__ == "__main__":
    main_cli()
