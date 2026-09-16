"""W1-004 acceptance for the four approved Roszdravnadzor channels (A-D).

The runner never fabricates companies. Bulk evidence must already be loaded by
``scripts.sync_roszdrav``; on-demand evidence is read from the dated cache unless
``--live`` is explicitly requested. Reports and screenshots are written below
the ignored ``data/acceptance`` directory.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
import platform
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from sqlalchemy import func, select, text

from app.database.postgres import get_session
from app.models.company import Company
from app.models.roszdrav import (
    RoszdravClinicalOrganizationEntry,
    RoszdravLicenseEntry,
    RoszdravMedicalDeviceCheck,
    RoszdravUnifiedLicenseCheck,
)
from app.models.source import DataSet, IngestionRun
from app.services.roszdrav_registry_service import (
    CLINICAL_ORG_DATASET,
    LICENSE_DATASETS,
    MEDICAL_DEVICE_DATASET,
    UNIFIED_LICENSE_DATASET,
)
from app.services.roszdrav_service import (
    get_cached_roszdrav_unified_license_check,
    get_roszdrav_bulk_license_check_for_inn,
    get_roszdrav_clinical_org_check_for_inn,
    get_roszdrav_medical_device_company_check,
    refresh_roszdrav_medical_device_check,
    refresh_roszdrav_unified_license_check,
)


ROOT = Path(__file__).resolve().parents[1]
LICENSE_FOUND_INN = "1658122916"
CLINICAL_FOUND_INN = "0275080530"
ABSENT_INN = "9102309919"
DEVICE_FOUND_NUMBER = "ФС 012а2004/0344-04"
DEVICE_ABSENT_NUMBER = "W1-004-NOT-FOUND"
EXPECTED_BULK = {
    LICENSE_DATASETS["pharma"]: (21433, 21433, 14644, "57cd27a979c7f5e91a52f15033a80ee524ed9ba1f965ea5d643aad6b180ee468"),
    LICENSE_DATASETS["narcotics"]: (7429, 7429, 1935, "266bc0e09ca46a14f4c67b0b40c63d95cb905e24c5c6bfd3250d22278f3ef8b3"),
    LICENSE_DATASETS["medical_device_maintenance"]: (2699, 2698, 2423, "cc58a7675680512ecb27b46715fb7f7ed663f0408131afb223ce366785d0bd74"),
}
EXPECTED_CLINICAL_SHA = "9db1ab2eec6b69f83cdb453806cc61e9244375e2378eb60d7771e484d2eb12e5"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def normalized_text(value):
    return " ".join(str(value or "").split())


def audit_bulk():
    evidence = {}
    with get_session() as session:
        master = session.scalar(select(func.count()).select_from(Company))
        for code, (expected_rows, expected_unique, expected_linked, checksum) in EXPECTED_BULK.items():
            dataset = session.scalar(select(DataSet).where(DataSet.code == code))
            require(dataset is not None and dataset.last_data_date == date(2026, 9, 13), f"{code}: wrong data date")
            rows = session.scalar(select(func.count()).select_from(RoszdravLicenseEntry).where(RoszdravLicenseEntry.dataset_id == dataset.id))
            unique = session.scalar(select(func.count(func.distinct(RoszdravLicenseEntry.inn))).where(RoszdravLicenseEntry.dataset_id == dataset.id))
            linked = session.scalar(select(func.count(func.distinct(RoszdravLicenseEntry.inn))).select_from(RoszdravLicenseEntry).join(Company, Company.inn == RoszdravLicenseEntry.inn).where(RoszdravLicenseEntry.dataset_id == dataset.id))
            run = session.scalar(select(IngestionRun).where(IngestionRun.dataset_id == dataset.id, IngestionRun.status == "success").order_by(IngestionRun.id.desc()).limit(1))
            require((rows, unique, linked) == (expected_rows, expected_unique, expected_linked), f"{code}: count/coverage mismatch")
            require(run is not None and run.file_checksum == checksum and run.rows_read == expected_rows and run.rows_inserted == expected_rows and run.errors_count == 0, f"{code}: ingestion evidence mismatch")
            details = run.details or {}
            require(details.get("source_file_date") == "2026-09-13" and details.get("official_metadata_data_date") == "2026-09-20" and details.get("metadata_date_after_file_date") is True, f"{code}: separate metadata dates missing")
            evidence[code] = {"rows": rows, "unique_inn": unique, "master_matches": linked, "master_companies": master, "data_date": str(dataset.last_data_date), "source_file_date": details["source_file_date"], "official_metadata_data_date": details["official_metadata_data_date"], "duplicates": run.rows_skipped, "rejected": details.get("rejected_records"), "sha256": checksum}

        dataset = session.scalar(select(DataSet).where(DataSet.code == CLINICAL_ORG_DATASET))
        rows = session.scalar(select(func.count()).select_from(RoszdravClinicalOrganizationEntry).where(RoszdravClinicalOrganizationEntry.dataset_id == dataset.id))
        unique = session.scalar(select(func.count(func.distinct(RoszdravClinicalOrganizationEntry.inn))).where(RoszdravClinicalOrganizationEntry.dataset_id == dataset.id))
        linked = session.scalar(select(func.count(func.distinct(RoszdravClinicalOrganizationEntry.inn))).select_from(RoszdravClinicalOrganizationEntry).join(Company, Company.inn == RoszdravClinicalOrganizationEntry.inn).where(RoszdravClinicalOrganizationEntry.dataset_id == dataset.id))
        run = session.scalar(select(IngestionRun).where(IngestionRun.dataset_id == dataset.id, IngestionRun.status == "success").order_by(IngestionRun.id.desc()).limit(1))
        require(dataset.last_data_date == date(2026, 9, 13) and (rows, unique, linked) == (294, 291, 45), "Clinical snapshot coverage mismatch")
        require(run.file_checksum == EXPECTED_CLINICAL_SHA and run.rows_read == 295 and run.rows_inserted == 294 and run.rows_skipped == 1 and run.errors_count == 0, "Clinical ingestion evidence mismatch")
        details = run.details or {}
        require(details.get("source_file_date") == "2026-09-06" and details.get("official_metadata_data_date") == "2026-09-13", "Clinical separate metadata dates missing")
        evidence[CLINICAL_ORG_DATASET] = {"source_rows": 295, "stored_rows": rows, "unique_inn": unique, "master_matches": linked, "master_companies": master, "data_date": str(dataset.last_data_date), "source_file_date": details["source_file_date"], "official_metadata_data_date": details["official_metadata_data_date"], "duplicates": 1, "rejected": 0, "sha256": EXPECTED_CLINICAL_SHA}
    return evidence


def audit_semantics(request_date):
    bulk_found = get_roszdrav_bulk_license_check_for_inn(LICENSE_FOUND_INN)
    bulk_absent = get_roszdrav_bulk_license_check_for_inn(ABSENT_INN)
    clinical_found = get_roszdrav_clinical_org_check_for_inn(CLINICAL_FOUND_INN)
    clinical_absent = get_roszdrav_clinical_org_check_for_inn(ABSENT_INN)
    unified_found = get_cached_roszdrav_unified_license_check(LICENSE_FOUND_INN, request_date)
    unified_absent = get_cached_roszdrav_unified_license_check(ABSENT_INN, request_date)
    unified_unchecked = get_cached_roszdrav_unified_license_check(CLINICAL_FOUND_INN, request_date)
    devices_company = get_roszdrav_medical_device_company_check()
    expected = ((bulk_found, "found"), (bulk_absent, "not_found"), (clinical_found, "found"), (clinical_absent, "not_found"), (unified_found, "found"), (unified_absent, "not_found"))
    for check, result in expected:
        require(check.get("checked") is True and check.get("result") == result, f"Expected {result}: {check}")
        require(check.get("matching_method") == "inn_exact", "Exact-INN evidence missing")
    require(unified_found.get("record_count") == 3 and unified_absent.get("record_count") == 0, "B found/not_found counts mismatch")
    require(unified_unchecked.get("result") == "unavailable" and unified_unchecked.get("reason") == "not_checked", "Unchecked must be unavailable")
    require(devices_company.get("result") == "not_applicable" and devices_company.get("applicable") is False, "C company state must be not_applicable")
    return {"A_found": bulk_found, "A_not_found": bulk_absent, "B_found": unified_found, "B_not_found": unified_absent, "B_unchecked": unified_unchecked, "C_company": devices_company, "D_found": clinical_found, "D_not_found": clinical_absent}


def audit_on_demand(request_date):
    with get_session() as session:
        master = session.scalar(select(func.count()).select_from(Company))
        b_rows = session.scalars(select(RoszdravUnifiedLicenseCheck).where(RoszdravUnifiedLicenseCheck.request_date == request_date)).all()
        c_rows = session.scalars(select(RoszdravMedicalDeviceCheck).where(RoszdravMedicalDeviceCheck.request_date == request_date)).all()
        by_inn = {row.inn: row for row in b_rows}
        by_number = {row.registration_number: row for row in c_rows}
        require(by_inn[LICENSE_FOUND_INN].result_status == "success" and by_inn[LICENSE_FOUND_INN].is_found is True and by_inn[LICENSE_FOUND_INN].http_status == 200 and by_inn[LICENSE_FOUND_INN].raw_payload, "B found PostgreSQL evidence missing")
        require(by_inn[ABSENT_INN].result_status == "success" and by_inn[ABSENT_INN].is_found is False and by_inn[ABSENT_INN].http_status == 200 and by_inn[ABSENT_INN].raw_payload, "B not_found PostgreSQL evidence missing")
        require(by_number[DEVICE_FOUND_NUMBER].result_status == "success" and by_number[DEVICE_FOUND_NUMBER].is_found is True and by_number[DEVICE_FOUND_NUMBER].http_status == 200 and by_number[DEVICE_FOUND_NUMBER].raw_payload, "C found PostgreSQL evidence missing")
        require(by_number[DEVICE_ABSENT_NUMBER].result_status == "success" and by_number[DEVICE_ABSENT_NUMBER].is_found is False and by_number[DEVICE_ABSENT_NUMBER].http_status == 200 and by_number[DEVICE_ABSENT_NUMBER].raw_payload, "C not_found PostgreSQL evidence missing")
        successful_b = [row for row in b_rows if row.result_status == "success"]
        linked_b = session.scalar(select(func.count(func.distinct(Company.id))).select_from(Company).join(RoszdravUnifiedLicenseCheck, RoszdravUnifiedLicenseCheck.inn == Company.inn).where(RoszdravUnifiedLicenseCheck.request_date == request_date, RoszdravUnifiedLicenseCheck.result_status == "success"))
        return {
            "B": {"mode": "ON_DEMAND_WITH_DATED_CACHE", "request_date": str(request_date), "cache_rows": len(b_rows), "success_rows": len(successful_b), "error_rows": len(b_rows) - len(successful_b), "checked_master_companies": linked_b, "unchecked_master_companies": master - linked_b, "full_registry_import": "N/A: no bulk crawl"},
            "C": {"mode": "ON_DEMAND_WITH_DATED_CACHE", "request_date": str(request_date), "cache_rows": len(c_rows), "success_rows": sum(row.result_status == "success" for row in c_rows), "error_rows": sum(row.result_status == "error" for row in c_rows), "company_coverage": "N/A: official response has no exact company INN/OGRN", "full_registry_import": "N/A: no bulk crawl"},
        }


def browser_check(output):
    from playwright.sync_api import sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    cases = [
        (LICENSE_FOUND_INN, "found", "Лицензии найдены по точному ИНН", "license-found"),
        (ABSENT_INN, "not_found", "Лицензии трёх открытых категорий не найдены", "license-not-found"),
        (CLINICAL_FOUND_INN, "not_found", "Организация включена в официальный перечень", "clinical-found"),
    ]
    results = []
    with (output / "server.log").open("w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, stdout=log, stderr=log)
        try:
            for _ in range(150):
                require(proc.poll() is None, "FastAPI failed to start")
                try:
                    if httpx.get(origin + "/openapi.json", timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError("FastAPI readiness timeout")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    for inn, bulk_result, required_text, label in cases:
                        page = browser.new_page(viewport={"width": 1440, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error, bucket=errors: bucket.append(str(error)))
                        response = page.goto(f"{origin}/company/{inn}", wait_until="networkidle", timeout=90000)
                        block = page.get_by_test_id("roszdrav")
                        block.wait_for(state="visible", timeout=15000)
                        visible = normalized_text(block.inner_text())
                        require(response is not None and response.status == 200, f"{inn}: company HTTP != 200")
                        require(block.get_attribute("data-result") == bulk_result and not errors, f"{inn}: browser state/page error")
                        require(required_text in visible and "Источник истины: Росздравнадзор" in visible, f"{inn}: required visible text missing")
                        require("Автопривязка к компании не применяется" in visible, "C not_applicable limitation is not visible")
                        screenshot = output / f"{label}.png"
                        block.screenshot(path=str(screenshot))
                        results.append({"inn": inn, "http_status": response.status, "bulk_result": bulk_result, "page_errors": errors, "visible_text": visible, "screenshot": str(screenshot), "browser": "Chromium", "browser_version": browser.version})
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


def main():
    parser = argparse.ArgumentParser(description="W1-004 real six-gate acceptance")
    parser.add_argument("--tests", action="store_true")
    parser.add_argument("--live", action="store_true", help="Refresh two B INNs and two C registration numbers")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    output = args.output_dir or ROOT / "data/acceptance/w1-004" / now.strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=True)
    gates = {str(number): "NOT CONFIRMED" for number in range(1, 7)}
    report = {"source": "W1-004", "scope": "Roszdravnadzor A+B+C+D", "executed_at": now.isoformat(), "platform": platform.system(), "gates": gates, "accepted": False, "user_mac_accepted": False, "auto_update": "NOT_CONFIGURED"}
    try:
        with get_session() as session:
            database = session.scalar(text("SELECT current_database()"))
            require(session.bind.dialect.name == "postgresql" and database == "kontragent", "Acceptance requires user PostgreSQL database kontragent")
            require(not os.getenv("GITHUB_ACTIONS"), "This runner is for user-local acceptance")
            report["database"] = database
            report["alembic"] = session.scalars(text("SELECT version_num FROM alembic_version")).all()
            require(report["alembic"] == ["a9c4e6f8b201"], "Wrong Alembic revision")
            for inn in (LICENSE_FOUND_INN, CLINICAL_FOUND_INN, ABSENT_INN):
                require(session.scalar(select(Company.id).where(Company.inn == inn)) is not None, f"Acceptance company {inn} is absent")
        report["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["git_status"] = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        if args.tests:
            test = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
            (output / "tests.log").write_text(test.stdout)
            report["tests"] = {"returncode": test.returncode, "summary": test.stdout.strip().splitlines()[-1:]}
            require(test.returncode == 0 and " passed" in test.stdout, "Full tests failed")
            gates["1"] = "PASS"
        request_date = date.today()
        if args.live:
            report["live"] = {
                "B_found": refresh_roszdrav_unified_license_check(LICENSE_FOUND_INN, request_date, force_refresh=True),
                "B_not_found": refresh_roszdrav_unified_license_check(ABSENT_INN, request_date, force_refresh=True),
                "C_found": refresh_roszdrav_medical_device_check(DEVICE_FOUND_NUMBER, request_date, force_refresh=True),
                "C_not_found": refresh_roszdrav_medical_device_check(DEVICE_ABSENT_NUMBER, request_date, force_refresh=True),
            }
        report["bulk"] = audit_bulk()
        report["semantics"] = audit_semantics(request_date)
        report["on_demand"] = audit_on_demand(request_date)
        gates["2"] = "PASS"
        gates["3"] = "PASS"
        gates["4"] = "PASS"
        gates["6"] = "PASS"
        if args.browser:
            report["browser"] = browser_check(output)
            gates["5"] = "PASS"
        report["accepted"] = all(value == "PASS" for value in gates.values())
        report["user_mac_accepted"] = report["accepted"] and platform.system() == "Darwin"
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)[:2000]
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = output / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print("Report:", path)
        print("W1-004 SIX GATES:", "PASS" if report["accepted"] else "NOT ACCEPTED")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
