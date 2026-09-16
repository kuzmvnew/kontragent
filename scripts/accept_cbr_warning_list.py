"""W1-001 acceptance on the configured PostgreSQL. No fixture or master writes.

--sync is the only data-write option. --browser launches Chromium against the
real FastAPI app on loopback. Without the requested evidence a gate is NOT_RUN,
never PASS. Reports/screenshots remain under ignored data/acceptance/.
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
from app.models.cbr_warning_list import CbrWarningListEntry as Entry
from app.models.source import DataSet, IngestionRun
from app.services.cbr_warning_list_service import get_cbr_warning_list_check_for_inn

ROOT = Path(__file__).resolve().parents[1]


def audit_snapshot() -> dict:
    """Read-only counts, provenance and two independent service reads."""
    with get_session() as session:
        if session.bind.dialect.name != "postgresql":
            raise RuntimeError("Acceptance requires PostgreSQL, not a SQLite substitute")
        dataset = session.scalar(select(DataSet).where(DataSet.code == "cbr_warning_list"))
        if dataset is None or dataset.last_data_date is None:
            raise RuntimeError("W1-001 has no published snapshot")
        scope = (Entry.dataset_id == dataset.id, Entry.data_date == dataset.last_data_date)
        count = session.scalar(select(func.count()).select_from(Entry).where(*scope))
        with_inn = session.scalar(select(func.count()).select_from(Entry).where(*scope, Entry.inn.is_not(None)))
        unique_inn = session.scalar(select(func.count(func.distinct(Entry.inn))).where(*scope))
        linked = session.execute(select(func.count(Entry.id), func.count(func.distinct(Company.id)))
                                 .select_from(Entry).join(Company, Company.inn == Entry.inn).where(*scope)).one()
        dates = session.execute(select(func.min(Entry.entry_date), func.max(Entry.entry_date),
                                       func.min(Entry.update_date), func.max(Entry.update_date)).where(*scope)).one()
        master_count = session.scalar(select(func.count()).select_from(Company))
        sample = session.execute(select(Company.inn, Company.name, Entry.cbr_id)
                                 .join(Entry, Entry.inn == Company.inn).where(*scope)
                                 .order_by(func.length(Company.inn), Company.inn, Entry.cbr_id).limit(1)).first()
        absence = session.scalar(select(Company.inn).where(
            func.length(Company.inn).in_([10, 12]),
            ~select(Entry.id).where(*scope, Entry.inn == Company.inn).exists()
        ).order_by(Company.id).limit(1))
        run = session.scalar(select(IngestionRun).where(
            IngestionRun.dataset_id == dataset.id, IngestionRun.status == "success",
            IngestionRun.data_date == dataset.last_data_date
        ).order_by(IngestionRun.finished_at.desc()).limit(1))
        latest = session.scalar(select(IngestionRun).where(IngestionRun.dataset_id == dataset.id)
                                .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc()).limit(1))
        report = {
            "database_engine": "PostgreSQL", "snapshot_records": count,
            "with_inn": with_inn, "without_inn": count - with_inn,
            "unique_inn": unique_inn, "linked_records": linked[0],
            "linked_master_companies": linked[1], "master_companies": master_count,
            "unmatched_records_with_inn": with_inn - linked[0],
            "retrieval_date": str(dataset.last_data_date),
            "date_basis": "retrieved_at_utc (not an official source cut-off date)",
            "entry_date_min": str(dates[0]), "entry_date_max": str(dates[1]),
            "update_date_min": str(dates[2]), "update_date_max": str(dates[3]),
            "last_success_at": str(dataset.last_success_at),
            "latest_attempt_status": latest.status if latest else None,
            "successful_run_id": run.id if run else None,
            "sha256": run.file_checksum if run else None,
            "ingestion_details": run.details if run else {},
            "ledger_matches_snapshot": bool(run and run.rows_inserted == count and count > 0),
            "sample": dict(zip(("inn", "name", "cbr_id"), sample)) if sample else None,
            "absence_inn": absence,
        }
        # Existing deployments might not use Alembic; report that explicitly.
        if session.scalar(text("SELECT to_regclass('alembic_version')")):
            report["alembic"] = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        else:
            report["alembic"] = "not present"
    if sample:
        first = get_cbr_warning_list_check_for_inn(sample.inn)
        second = get_cbr_warning_list_check_for_inn(sample.inn)
        report["reread_pass"] = bool(first == second and first["result"] == "found"
                                     and first["record_count"] > 0)
        report["found_check"] = first
    else:
        report["reread_pass"] = False
    report["absence_check"] = get_cbr_warning_list_check_for_inn(absence) if absence else None
    return report


def browser_check(cases: list[dict], output: Path) -> list[dict]:
    """Exercise /company/{inn}, not a rendered template or a mocked HTTP client."""
    from playwright.sync_api import sync_playwright
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    results = []
    with (output / "server.log").open("w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                                 "--port", str(port)], cwd=ROOT, stdout=log, stderr=log)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("FastAPI did not start; see local server.log")
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
                        response = page.goto(f"{origin}/company/{case['inn']}", wait_until="networkidle", timeout=60000)
                        block = page.get_by_test_id("cbr-warning-list")
                        block.wait_for(state="visible")
                        actual = block.get_attribute("data-result")
                        content = block.inner_text()
                        if response.status != 200 or actual != case["result"] or errors:
                            raise AssertionError(f"Browser case {case['label']} failed: HTTP {response.status}, {actual}")
                        if case.get("text") and case["text"] not in content:
                            raise AssertionError("Expected source text is not visible")
                        if actual == "found" and not block.locator('a[href^="https://www.cbr.ru/inside/warning-list/detail/"]').count():
                            raise AssertionError("Official evidence link is missing")
                        path = output / f"{case['label']}.png"
                        block.screenshot(path=str(path))
                        results.append({"label": case["label"], "inn": case["inn"], "http_status": response.status,
                                        "result": actual, "visible_text": content, "screenshot": str(path), "page_errors": errors})
                        page.close()
                finally:
                    browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="W1-001: six evidence gates; never modifies Master Registry")
    parser.add_argument("--sync", action="store_true", help="Fetch and atomically replace W1-001 only")
    parser.add_argument("--tests", action="store_true", help="Run the complete offline test suite")
    parser.add_argument("--browser", action="store_true", help="Open real local company pages in Chromium")
    parser.add_argument("--environment", default="user-local", help="Evidence environment label, never a database URL")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or ROOT / "data/acceptance/w1-001" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    gates = {str(i): "NOT_RUN" for i in range(1, 7)}
    report = {"source": "W1-001", "environment": args.environment, "gates": gates,
              "executed_at": datetime.now(timezone.utc).isoformat(), "auto_update": "NOT_CONFIGURED"}
    try:
        if args.tests:
            with (output / "tests.log").open("w") as log:
                result = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            gates["1"] = "PASS" if result.returncode == 0 else "FAIL"
            if result.returncode:
                raise RuntimeError("Automated tests failed; sync not started")
        if args.sync:
            from scripts.sync_cbr_warning_list import sync_cbr_warning_list
            report["live_import"] = sync_cbr_warning_list()
        snapshot = audit_snapshot()
        report["snapshot"] = snapshot
        gates["3"] = "PASS" if snapshot["reread_pass"] else "NOT_CONFIRMED"
        evidence = snapshot["ingestion_details"] or {}
        gates["2"] = "PASS" if (args.sync and snapshot["reread_pass"] and evidence.get("http_status") == 200) else "NOT_CONFIRMED"
        gates["6"] = "PASS" if (snapshot["ledger_matches_snapshot"] and evidence.get("complete_snapshot_validated")) else "NOT_CONFIRMED"
        negative = snapshot["absence_check"]
        gates["4"] = "PASS" if (gates["1"] == "PASS" and negative and negative["result"] == "not_found"
                                  and negative["checked"] and negative["is_listed"] is False) else "NOT_CONFIRMED"
        report["state_semantics"] = {
            "absence": "checked against this dated snapshot only",
            "non_applicability": "N/A: source applies to both legal entities and individual entrepreneurs",
            "failure_tests": "offline regression tests; no deliberate outage of user database",
            "missing_inn": "cannot match these entries by exact INN; not proof of safety",
        }
        if args.browser and snapshot["sample"] and snapshot["absence_inn"]:
            report["browser"] = browser_check([
                {"label": "found", "inn": snapshot["sample"]["inn"], "result": "found"},
                {"label": "not-found", "inn": snapshot["absence_inn"], "result": "not_found"},
            ], output)
            gates["5"] = "PASS"
        report["accepted"] = all(value == "PASS" for value in gates.values())
    except Exception as error:
        # No credentials / raw SQL in the user-shareable report.
        report["error_type"] = type(error).__name__
        report["accepted"] = False
    path = output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("Report:", path)
    print("W1-001 SIX GATES:", "PASS" if report["accepted"] else "NOT ACCEPTED")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
