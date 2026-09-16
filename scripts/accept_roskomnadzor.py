"""W1-005 six-gate database/privacy/semantic/browser acceptance."""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time

import httpx
from sqlalchemy import func, select, text

from app.database.postgres import get_session
from app.models.company import Company
from app.models.roskomnadzor import RoskomnadzorCompanyFact, RoskomnadzorPrivatePersonRecord
from app.models.source import DataSet, IngestionRun
from app.services.roskomnadzor_registry_service import DATASETS
from app.services.roskomnadzor_service import get_cached_pd_operator_check, get_roskomnadzor_bulk_check_for_inn


def require(value, message):
    if not value: raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--tests", action="store_true")
    args = parser.parse_args()
    report = {"revision": "c5e1a2b3d4f5", "auto_update": "NOT_CONFIGURED", "channels": {}, "gates": {str(i): "NOT CONFIRMED" for i in range(1, 7)}}
    if args.tests:
        test = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        require(test.returncode == 0 and " passed" in test.stdout, "Full tests failed")
        report["tests"] = test.stdout.strip().splitlines()[-1]
        report["gates"]["1"] = "PASS"
    with get_session() as session:
        require(session.scalar(text("select current_database()")) == "kontragent", "Acceptance requires PostgreSQL kontragent")
        require(session.scalars(text("select version_num from alembic_version")).all() == ["c5e1a2b3d4f5"], "Wrong Alembic revision")
        leaks = session.scalar(select(func.count()).select_from(RoskomnadzorPrivatePersonRecord).where(RoskomnadzorPrivatePersonRecord.is_published.is_(True)))
        require(leaks == 0, "Private records marked for publication")
        for channel, code in DATASETS.items():
            dataset = session.scalar(select(DataSet).where(DataSet.code == code))
            require(dataset is not None, f"Missing dataset {code}")
            if channel == "pd_operators":
                report["channels"][channel] = {"mode": "ON_DEMAND_WITH_DATED_CACHE"}
                continue
            public = session.scalar(select(func.count()).select_from(RoskomnadzorCompanyFact).where(RoskomnadzorCompanyFact.dataset_id == dataset.id))
            private = session.scalar(select(func.count()).select_from(RoskomnadzorPrivatePersonRecord).where(RoskomnadzorPrivatePersonRecord.dataset_id == dataset.id))
            run = session.scalar(select(IngestionRun).where(IngestionRun.dataset_id == dataset.id, IngestionRun.status == "success").order_by(IngestionRun.id.desc()).limit(1))
            require(dataset.last_data_date is not None, f"Dataset not loaded: {code}")
            require(run and run.rows_read == public + private + run.rows_skipped and run.errors_count == 0, f"Count mismatch: {code}")
            linked = session.scalar(select(func.count(func.distinct(RoskomnadzorCompanyFact.inn))).select_from(RoskomnadzorCompanyFact).join(Company, Company.inn == RoskomnadzorCompanyFact.inn).where(RoskomnadzorCompanyFact.dataset_id == dataset.id))
            report["channels"][channel] = {"status": "LOADED", "data_date": str(dataset.last_data_date), "public": public, "private": private, "master_matches": linked, "sha256": run.file_checksum}
    found_cases = {"communications": "6320002223", "broadcast": "4026009238", "media": "7814355929", "information_distributors": "7736207543", "hosting": "7702233760"}
    for channel, inn in found_cases.items():
        check = get_roskomnadzor_bulk_check_for_inn(inn, channel)
        require(check["result"] == "found" and check["matching_method"] == "inn_exact", f"Expected found: {channel}")
    for channel in DATASETS:
        if channel == "pd_operators": continue
        check = get_roskomnadzor_bulk_check_for_inn("9102309919", channel)
        require(check["result"] == "not_found", f"Expected not_found: {channel}")
    require(get_cached_pd_operator_check("9706063520")["result"] == "found", "PD found cache missing")
    require(get_cached_pd_operator_check("9102309919")["result"] == "not_found", "PD not_found cache missing")
    report["gates"].update({"2": "PASS", "3": "PASS", "4": "PASS", "6": "PASS"})
    if args.browser:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=1).status_code == 200: break
                except httpx.RequestError: pass
                time.sleep(.2)
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
                    response = page.goto(f"http://127.0.0.1:{port}/company/6320002223", wait_until="networkidle", timeout=90000)
                    block = page.get_by_test_id("roskomnadzor"); block.wait_for(state="visible")
                    text_value = " ".join(block.inner_text().split())
                    require(response and response.status == 200 and not errors, "Browser HTTP/page error")
                    require("Лицензии связи" in text_value and "Источник истины: Роскомнадзор" in text_value and "Данные физлиц и ИП хранятся отдельно" in text_value, "Browser evidence missing")
                    report["browser"] = {"engine": "Chromium", "version": browser.version, "http": response.status, "page_errors": errors}
                finally: browser.close()
            report["gates"]["5"] = "PASS"
        finally:
            proc.terminate(); proc.wait(timeout=10)
    report["accepted"] = all(value == "PASS" for value in report["gates"].values())
    print(report)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
