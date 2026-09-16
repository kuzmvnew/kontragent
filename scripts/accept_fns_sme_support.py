"""W1-003 six-gate acceptance on the configured PostgreSQL.

Without --sync this script is read-only. Browser mode opens actual FastAPI company
pages. Person/NPD-only source rows are never inserted into Master Registry or the
public company card.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, select, text

from app.database.postgres import get_session
from app.models.company import Company
from app.models.fns_sme_support import FnsSmeSupportEntry as Entry
from app.models.source import DataSet, IngestionRun
from app.services.fns_sme_support_service import get_fns_sme_support_check_for_inn

ROOT = Path(__file__).resolve().parents[1]


def audit_snapshot() -> dict:
    with get_session() as session:
        if session.bind.dialect.name != "postgresql":
            raise RuntimeError("Acceptance requires PostgreSQL")
        dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_sme_support"))
        if dataset is None:
            raise RuntimeError("W1-003 dataset is not registered")
        run = session.scalar(
            select(IngestionRun).where(
                IngestionRun.dataset_id == dataset.id,
                IngestionRun.status == "success",
            ).order_by(IngestionRun.finished_at.desc().nullslast(), IngestionRun.id.desc()).limit(1)
        )
        if run is None or not (run.details or {}).get("complete_snapshot"):
            raise RuntimeError("W1-003 has no complete published snapshot")
        scope = (Entry.ingestion_run_id == run.id,)
        stored = session.scalar(select(func.count()).select_from(Entry).where(*scope))
        unique_inn = session.scalar(select(func.count(func.distinct(Entry.recipient_inn))).where(*scope))
        legal_records = session.scalar(select(func.count()).select_from(Entry).where(*scope, Entry.recipient_kind == "legal"))
        ip_records = session.scalar(select(func.count()).select_from(Entry).where(*scope, Entry.recipient_kind == "individual_entrepreneur_or_kfh"))
        violation_records = session.scalar(select(func.count()).select_from(Entry).where(*scope, Entry.violation_code == "1"))
        dates = session.execute(
            select(
                func.min(Entry.decision_date), func.max(Entry.decision_date),
                func.min(Entry.information_date), func.max(Entry.information_date),
            ).where(*scope)
        ).one()
        master_count = session.scalar(select(func.count()).select_from(Company))
        linked = session.execute(
            select(func.count(Entry.id), func.count(func.distinct(Company.id)))
            .select_from(Entry).join(Company, Company.inn == Entry.recipient_inn)
            .where(*scope)
        ).one()
        sample = session.execute(
            select(Company.inn, Company.name, Entry.source_document_id)
            .join(Entry, Entry.recipient_inn == Company.inn)
            .where(*scope)
            .order_by(func.length(Company.inn), Company.inn, Entry.source_document_id)
            .limit(1)
        ).first()
        absence = session.execute(
            select(Company.inn, Company.name).where(
                func.length(Company.inn).in_([10, 12]),
                ~select(Entry.id).where(*scope, Entry.recipient_inn == Company.inn).exists(),
            ).order_by(Company.id).limit(1)
        ).first()
        latest = session.scalar(
            select(IngestionRun).where(IngestionRun.dataset_id == dataset.id)
            .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc()).limit(1)
        )
        details = run.details or {}
        report = {
            "database_engine": "PostgreSQL",
            "data_date": str(run.data_date),
            "stored_company_ip_records": stored,
            "source_records": details.get("source_records"),
            "excluded_npd_person_records": details.get("excluded_npd_records"),
            "unique_company_ip_inn": unique_inn,
            "legal_records": legal_records,
            "ip_or_kfh_records": ip_records,
            "violation_records": violation_records,
            "decision_date_min": str(dates[0]),
            "decision_date_max": str(dates[1]),
            "information_date_min": str(dates[2]),
            "information_date_max": str(dates[3]),
            "master_companies": master_count,
            "linked_records": linked[0],
            "linked_master_companies": linked[1],
            "successful_run_id": run.id,
            "sha256": run.file_checksum,
            "source_url": run.source_url,
            "ingestion_details": details,
            "ledger_matches_snapshot": bool(stored == run.rows_inserted == details.get("eligible_records") and stored > 0),
            "latest_attempt_status": latest.status if latest else None,
            "sample": dict(zip(("inn", "name", "source_document_id"), sample)) if sample else None,
            "absence": dict(zip(("inn", "name"), absence)) if absence else None,
        }
        if session.scalar(text("SELECT to_regclass('alembic_version')")):
            report["alembic"] = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        else:
            report["alembic"] = "not present"

    if sample:
        first = get_fns_sme_support_check_for_inn(sample.inn)
        second = get_fns_sme_support_check_for_inn(sample.inn)
        report["reread_pass"] = bool(first == second and first["result"] == "found" and first["record_count"] > 0)
        report["found_check"] = first
    else:
        report["reread_pass"] = False
    report["absence_check"] = (
        get_fns_sme_support_check_for_inn(absence.inn) if absence else None
    )
    return report


def browser_check(cases: list[dict], output: Path) -> list[dict]:
    from playwright.sync_api import sync_playwright
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    results = []
    with (output / "server.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, stdout=log, stderr=log,
        )
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("FastAPI did not start")
                try:
                    if httpx.get(origin + "/openapi.json", timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("FastAPI readiness timeout")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    for case in cases:
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        response = page.goto(
                            f"{origin}/company/{case['inn']}",
                            wait_until="networkidle", timeout=60000,
                        )
                        block = page.get_by_test_id("fns-sme-support")
                        block.wait_for(state="visible")
                        actual = block.get_attribute("data-result")
                        content = block.inner_text()
                        if response.status != 200 or actual != case["result"] or errors:
                            raise AssertionError(
                                f"Browser {case['label']} failed: HTTP {response.status}, result={actual}, errors={errors}"
                            )
                        if case.get("text") and case["text"] not in content:
                            raise AssertionError("Expected source text is not visible")
                        if not block.locator('a[href^="https://www.nalog.gov.ru/opendata/7707329152-rsmppp/"]').count():
                            raise AssertionError("Official FNS evidence link is missing")
                        screenshot = output / f"{case['label']}.png"
                        block.screenshot(path=str(screenshot))
                        results.append(
                            {
                                "label": case["label"], "inn": case["inn"],
                                "http_status": response.status, "result": actual,
                                "visible_text": content, "screenshot": str(screenshot),
                                "page_errors": errors,
                            }
                        )
                        page.close()
                finally:
                    browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="W1-003 six-gate acceptance")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--tests", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--environment", default="user-local")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or ROOT / "data/acceptance/w1-003" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    gates = {str(i): "NOT_RUN" for i in range(1, 7)}
    report = {
        "source": "W1-003", "environment": args.environment,
        "gates": gates, "executed_at": datetime.now(timezone.utc).isoformat(),
        "auto_update": "NOT_CONFIGURED",
    }
    try:
        if args.tests:
            with (output / "tests.log").open("w") as log:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "tests", "-q"],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                )
            gates["1"] = "PASS" if result.returncode == 0 else "FAIL"
            if result.returncode:
                raise RuntimeError("Automated tests failed; sync not started")
        if args.sync:
            from scripts.sync_fns_sme_support import sync_fns_sme_support
            report["live_import"] = sync_fns_sme_support(force=args.force)
        snapshot = audit_snapshot()
        report["snapshot"] = snapshot
        gates["3"] = "PASS" if snapshot["reread_pass"] else "NOT_CONFIRMED"
        details = snapshot["ingestion_details"] or {}
        official_live = (
            snapshot["source_url"]
            and str(snapshot["source_url"]).startswith("https://file.nalog.ru/opendata/7707329152-rsmppp/")
            and details.get("http_status") == 200
            and details.get("complete_snapshot")
        )
        gates["2"] = "PASS" if (snapshot["reread_pass"] and official_live) else "NOT_CONFIRMED"
        absence = snapshot["absence_check"]
        gates["4"] = "PASS" if (
            gates["1"] == "PASS" and absence and absence["checked"]
            and absence["result"] == "not_found"
            and absence["is_support_recipient"] is False
        ) else "NOT_CONFIRMED"
        gates["6"] = "PASS" if (
            snapshot["ledger_matches_snapshot"]
            and details.get("complete_snapshot")
            and snapshot["source_records"]
            and snapshot["master_companies"]
        ) else "NOT_CONFIRMED"
        report["state_semantics"] = {
            "not_found": "absence of exact-INN company/IP support fact in this complete dated snapshot only",
            "not_applicable": "N/A for a valid legal-entity/IP INN; NPD-only person rows are outside Company scope and not persisted",
            "failure": "unavailable; failed/running snapshot is never used as a negative result",
        }
        if args.browser and snapshot["sample"] and snapshot["absence"]:
            report["browser"] = browser_check(
                [
                    {"label": "found", "inn": snapshot["sample"]["inn"], "result": "found"},
                    {"label": "not-found", "inn": snapshot["absence"]["inn"], "result": "not_found"},
                ], output,
            )
            gates["5"] = "PASS"
        report["accepted"] = all(value == "PASS" for value in gates.values())
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)[:1000]
        report["accepted"] = False
    path = output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("Report:", path)
    print("W1-003 SIX GATES:", "PASS" if report["accepted"] else "NOT ACCEPTED")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
