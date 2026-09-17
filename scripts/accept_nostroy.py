from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import subprocess
import time

from playwright.sync_api import sync_playwright
from sqlalchemy import func, select, text

from app.database.postgres import get_session
from app.models.company import Company
from app.models.nostroy import NoprizMemberCheck, NostroyMemberCheck, SroPersonRegistryRecord


FOUND_INN = "5907056036"
NOT_FOUND_INN = "9102309919"
PRODUCT_FOUND_INN = "7810978295"


def _coverage():
    session = get_session()
    try:
        master = session.scalar(select(func.count()).select_from(Company))
        nostroy = session.scalars(select(NostroyMemberCheck)).all()
        nopriz = session.scalars(select(NoprizMemberCheck)).all()
        checked = {row.inn for row in [*nostroy, *nopriz]}
        master_matches = session.scalar(select(func.count(func.distinct(Company.inn))).where(Company.inn.in_(checked))) if checked else 0
        private_count = session.scalar(select(func.count()).select_from(SroPersonRegistryRecord))
        public_count = session.scalar(select(func.count()).select_from(SroPersonRegistryRecord).where(SroPersonRegistryRecord.public_visibility.is_(True)))
        linked_count = session.scalar(select(func.count()).select_from(SroPersonRegistryRecord).where(SroPersonRegistryRecord.company_inn.is_not(None)))
        return {
            "alembic": session.execute(text("select version_num from alembic_version")).scalar_one(),
            "master": master,
            "nostroy_cache": len(nostroy),
            "nostroy_success": sum(row.result_status == "success" for row in nostroy),
            "nostroy_found": sum(row.is_found is True for row in nostroy),
            "nostroy_not_found": sum(row.is_found is False for row in nostroy),
            "nopriz_cache": len(nopriz),
            "nopriz_success": sum(row.result_status == "success" for row in nopriz),
            "nopriz_error": sum(row.result_status == "error" for row in nopriz),
            "nopriz_found": sum(row.is_found is True for row in nopriz),
            "unique_checked_inns": len(checked),
            "master_matches": master_matches,
            "unchecked_master": master - master_matches,
            "private_person_records": private_count,
            "public_person_records": public_count,
            "linked_person_records": linked_count,
            "manual_review_person_records": session.scalar(select(func.count()).select_from(SroPersonRegistryRecord).where(SroPersonRegistryRecord.relationship_status == "manual_review_required")),
        }
    finally:
        session.close()


def _browser(base_url, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for inn in (FOUND_INN, NOT_FOUND_INN, PRODUCT_FOUND_INN):
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            errors = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            response = page.goto(f"{base_url}/company/{inn}", wait_until="networkidle", timeout=90_000)
            block = page.get_by_test_id("sro-registries")
            block.wait_for(state="visible")
            text_value = " ".join(block.inner_text().split())
            result[inn] = {
                "http": response.status,
                "page_errors": errors,
                "nostroy": block.get_by_test_id("sro-nostroy").get_attribute("data-result"),
                "nopriz": block.get_by_test_id("sro-nopriz").get_attribute("data-result"),
                "exact_inn_visible": "точному ИНН" in text_value or "exact ИНН" in text_value,
                "date_visible": "Дата проверки:" in text_value,
                "private_person_exposed": "Private Specialist" in page.content(),
            }
            block.screenshot(path=str(output_dir / f"{inn}.png"))
            page.close()
        browser.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    tests = None
    if not args.skip_tests:
        completed = subprocess.run(["uv", "run", "python", "-m", "pytest", "tests", "-q"], text=True, capture_output=True)
        tests = {"returncode": completed.returncode, "last_line": completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else completed.stderr.strip()}
    coverage = _coverage()
    browser = _browser(args.base_url, Path("data/acceptance/w1-006"))
    gates = {
        "gate_1": tests is None or tests["returncode"] == 0,
        "gate_2": coverage["nostroy_success"] >= 2 and coverage["nopriz_success"] >= 1 and coverage["private_person_records"] >= 1,
        "gate_3": coverage["alembic"] == "e7a8b9c0d1e2" and coverage["nostroy_cache"] >= 2 and coverage["nopriz_cache"] >= 2,
        "gate_4": coverage["nostroy_found"] >= 1 and coverage["nostroy_not_found"] >= 1 and coverage["nopriz_error"] >= 1,
        "gate_5": all(row["http"] == 200 and row["page_errors"] == [] and not row["private_person_exposed"] for row in browser.values()) and browser[FOUND_INN]["nostroy"] == "found" and browser[NOT_FOUND_INN]["nostroy"] == "not_found" and browser[PRODUCT_FOUND_INN]["nopriz"] == "found",
        "gate_6": coverage["unique_checked_inns"] == 3 and coverage["public_person_records"] == 0,
    }
    report = {"date": date.today().isoformat(), "duration_seconds": round(time.monotonic() - started, 2), "tests": tests, "coverage": coverage, "browser": browser, "gates": gates, "accepted": all(gates.values())}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("W1-006 SIX GATES: PASS" if report["accepted"] else "W1-006 SIX GATES: NOT CONFIRMED")
    raise SystemExit(0 if report["accepted"] else 1)


if __name__ == "__main__":
    main()
