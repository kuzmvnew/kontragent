"""Disposable GitHub-hosted PostgreSQL/Chromium proof, NEVER the user's database.

Deterministic fixture tests are separate from the single live official import.
Only a ten-digit official INN/name is inserted into this isolated test master.
No bulk data, database credentials or personal (12-digit) sample is archived.
"""
import json
import os
from pathlib import Path

from sqlalchemy import delete, func, select

from app.database.base import Base
from app.database.postgres import engine, get_session
import app.models
from app.models.company import Company
from app.models.cbr_warning_list import CbrWarningListEntry as Entry
from app.models.source import DataSet, IngestionRun
from app.providers.cbr_warning_list_provider import CbrWarningListProviderError, FULL_LIST_JSON_URL
from app.services.cbr_warning_registry_service import ensure_cbr_warning_list_dataset
from scripts.accept_cbr_warning_list import audit_snapshot, browser_check
from scripts.sync_cbr_warning_list import sync_cbr_warning_list

OUT = Path("data/acceptance/w1-001-ci")


def require_disposable_ci():
    if (os.getenv("GITHUB_ACTIONS") != "true" or engine.url.database != "test"
            or engine.url.host not in {"localhost", "127.0.0.1"}):
        raise RuntimeError("Fixture writes require GitHub-hosted CI and loopback PostgreSQL database 'test'")


class FixtureProvider:
    def __init__(self, *, bad=False, fail=False):
        self.bad, self.fail = bad, fail
    def fetch_full_list(self):
        if self.fail:
            raise CbrWarningListProviderError(kind="timeout", message="synthetic fixture timeout")
        rows = [{"id": "X" * 81 if self.bad else "1", "nameOrg": "SYNTHETIC W1-001 TEST COMPANY",
                 "inn": "7701234567", "dt": "01.01.2020", "Signs": [{"signRus": "SYNTHETIC TEST SIGNAL"}]}]
        return {"payload": rows, "raw_content": json.dumps(rows).encode(), "http_status": 200,
                "source_url": FULL_LIST_JSON_URL}


def fingerprint():
    with get_session() as s:
        dataset = s.scalar(select(DataSet).where(DataSet.code == "cbr_warning_list"))
        entries = s.execute(select(Entry.cbr_id, Entry.inn).where(Entry.dataset_id == dataset.id)).all()
        return entries, dataset.last_data_date, dataset.last_success_at


def fixtures():
    require_disposable_ci()
    # Legacy domains were historically created outside Alembic. This is ONLY
    # the disposable fixture schema, not a production migration/repair.
    Base.metadata.create_all(engine)
    ensure_cbr_warning_list_dataset()
    with get_session() as s:
        assert s.scalar(select(func.count()).select_from(Company)) == 0
        s.add_all([Company(inn="7701234567", name="SYNTHETIC W1-001 TEST COMPANY", entity_type="legal"),
                   Company(inn="7812345678", name="SYNTHETIC ABSENCE CONTROL", entity_type="legal")])
        s.commit()
    unavailable = browser_check([{"label": "fixture-not-loaded", "inn": "7701234567", "result": "unavailable"}], OUT)
    sync_cbr_warning_list(provider=FixtureProvider())
    first = audit_snapshot()
    assert first["snapshot_records"] == 1 and first["reread_pass"] and first["ledger_matches_snapshot"]
    sync_cbr_warning_list(provider=FixtureProvider())
    assert audit_snapshot()["snapshot_records"] == 1  # idempotent content, no duplicate growth
    before = fingerprint()
    try:
        sync_cbr_warning_list(provider=FixtureProvider(bad=True))
    except Exception:
        pass
    else:
        raise AssertionError("Oversized CBR ID must fail at PostgreSQL insert")
    assert before == fingerprint(), "Rollback must preserve both rows and publication markers"
    try:
        sync_cbr_warning_list(provider=FixtureProvider(fail=True))
    except CbrWarningListProviderError:
        pass
    else:
        raise AssertionError("Provider timeout must fail")
    assert before == fingerprint()
    assert audit_snapshot()["latest_attempt_status"] == "failed"
    degraded = browser_check([
        {"label": "fixture-failed-refresh-found", "inn": "7701234567", "result": "unavailable"},
        {"label": "fixture-failed-refresh-absent", "inn": "7812345678", "result": "unavailable"},
    ], OUT)
    # A failed refresh is a visible error signal even though the previously
    # published rows remain intact.  Recover with the same accepted snapshot
    # before proving positive and clean-negative product semantics again.
    sync_cbr_warning_list(provider=FixtureProvider())
    recovered = audit_snapshot()
    assert recovered["reread_pass"] and recovered["latest_attempt_status"] == "success"
    pages = browser_check([
        {"label": "fixture-found", "inn": "7701234567", "result": "found", "text": "SYNTHETIC TEST SIGNAL"},
        {"label": "fixture-absent", "inn": "7812345678", "result": "not_found"},
    ], OUT)
    with get_session() as s:
        dataset = s.scalar(select(DataSet).where(DataSet.code == "cbr_warning_list"))
        dataset.enabled = False
        s.commit()
    disabled = browser_check([{"label": "fixture-disabled", "inn": "7701234567", "result": "unavailable"}], OUT)
    with get_session() as s:
        s.scalar(select(DataSet).where(DataSet.code == "cbr_warning_list")).enabled = True
        s.commit()
    return {"evidence_kind": "SYNTHETIC_FIXTURES_NOT_LIVE_COMPANIES", "postgres": "PASS",
            "rollback_after_delete": "PASS", "repeat_import_no_duplicates": "PASS",
            "fetch_failure_logged_old_snapshot_preserved": "PASS",
            "failed_refresh_blocks_clean_semantics": "PASS",
            "same_snapshot_recovery": "PASS",
            "browser": unavailable + degraded + pages + disabled}


def live():
    require_disposable_ci()
    imported = sync_cbr_warning_list()  # one official full-list request, no API crawling
    with get_session() as s:
        # A real company identity comes only from the live official payload;
        # the other millions of the user's master registry are NOT copied here.
        record = s.scalar(select(Entry).where(func.length(Entry.inn) == 10, Entry.name.is_not(None))
                          .order_by(Entry.inn, Entry.cbr_id).limit(1))
        if record is None:
            return {"status": "NOT_ACCEPTED", "reason": "official_snapshot_has_no_ten_digit_inn_sample",
                    "import": imported}
        inn, cbr_id = record.inn, record.cbr_id
        existing = s.scalar(select(Company).where(Company.inn == inn))
        if existing is None:
            s.add(Company(inn=inn, name=record.name[:500], entity_type="legal", source="cbr_ci_sample"))
            s.commit()
    audit = audit_snapshot()
    from app.services.cbr_warning_list_service import get_cbr_warning_list_check_for_inn
    check1 = get_cbr_warning_list_check_for_inn(inn)
    check2 = get_cbr_warning_list_check_for_inn(inn)
    assert check1 == check2 and check1["result"] == "found"
    pages = browser_check([{"label": "live-company-found", "inn": inn, "result": "found"}], OUT)
    return {"status": "PASS_IN_DISPOSABLE_CI_ONLY", "import": imported,
            "sample": {"inn": inn, "cbr_id": cbr_id}, "reread": "PASS", "browser": pages,
            "coverage": {k: audit[k] for k in ("snapshot_records", "with_inn", "without_inn", "unique_inn",
                         "entry_date_min", "entry_date_max", "update_date_min", "update_date_max", "retrieval_date")},
            "user_master_coverage": "NOT_MEASURED", "user_mac_database": "NOT_ACCESSED"}


def main():
    require_disposable_ci()
    OUT.mkdir(parents=True, exist_ok=True)
    result = {"environment": "disposable-github-actions-postgresql-17", "user_mac_database": "NOT_ACCESSED"}
    try:
        result["fixtures"] = fixtures()
        try:
            result["live"] = live()
        except Exception as error:
            result["live"] = {"status": "NOT_ACCEPTED", "error_type": type(error).__name__,
                              "error_kind": getattr(error, "kind", None), "http_status": getattr(error, "http_status", None)}
    finally:
        (OUT / "ci-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("live", {}).get("status") != "PASS_IN_DISPOSABLE_CI_ONLY":
        print("W1-001 LIVE acceptance NOT PASSED; deterministic code tests alone are not full acceptance.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
