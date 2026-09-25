from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import cbr_warning_worker as worker
from app.models.cbr_warning_list import CbrWarningListEntry
from app.models.company import Company
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerPublicationState
from app.services import data_readiness_scheduler as scheduler


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, rows):
        self.raw = json.dumps({"RC": rows}, ensure_ascii=False).encode()

    def fetch_full_list(self):
        return {
            "payload": json.loads(self.raw),
            "raw_content": self.raw,
            "http_status": 200,
            "source_url": worker.FULL_LIST_JSON_URL,
            "headers": {
                "content-type": "application/json; charset=utf-8",
                "content-length": str(len(self.raw)),
            },
        }


class FakeContext:
    def __init__(self, raw_root: Path, *, discovered_at=NOW):
        self.schedule_metadata = {
            "source_url": worker.FULL_LIST_JSON_URL,
            "raw_root": str(raw_root),
            "discovered_at": discovered_at.isoformat(),
        }
        self.heartbeats = 0
        self.progress = []

    def ensure_active(self, *, now):
        assert now.tzinfo is not None

    def heartbeat(self):
        self.heartbeats += 1

    def report_counters(self, counters):
        self.progress.append(counters)


def _rows(inn="7701234567"):
    return [
        {
            "Id": 1001,
            "DT": "2026-09-23",
            "DateUpdate": "2026-09-24T09:10:11",
            "Name": "ООО ПРИМЕР",
            "INN": inn,
            "ADDR": "Москва",
            "Site": "example.test",
            "Sign": "Признаки нелегального кредитора",
            "Closed": False,
            "OrgType": "Юридическое лицо",
        },
        {
            "Id": 1002,
            "DT": "2026-09-24",
            "DateUpdate": "2026-09-24T10:11:12",
            "Name": "PROJECT.EXAMPLE",
            "INN": "",
            "Site": "project.example",
            "Sign": "Признаки финансовой пирамиды",
            "Closed": False,
            "OrgType": "Интернет-проект",
        },
    ]


@pytest.fixture
def cbr_db():
    connection = engine.connect()
    transaction = connection.begin()
    assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def test_handler_persists_checksum_addressed_raw_and_separates_dates(tmp_path):
    provider = FakeProvider(_rows())
    first = worker.run_cbr_warning_handler(
        FakeContext(tmp_path), provider=provider
    )
    second = worker.run_cbr_warning_handler(
        FakeContext(tmp_path, discovered_at=NOW + timedelta(days=1)),
        provider=provider,
    )

    validation = first.staging_result.validation.metadata
    assert validation["source_data_date"] == "2026-09-24"
    assert validation["official_actual_until"] == "2026-09-24"
    assert validation["source_records"] == 2
    assert validation["with_inn"] == 1
    assert first.counters.records_seen == 2
    assert first.counters.records_rejected == 0
    assert first.raw_artifacts[0].artifact_reference == second.raw_artifacts[0].artifact_reference
    assert first.staging_result.staging_pointer == second.staging_result.staging_pointer
    assert second.staging_result.validation.metadata["official_actual_until"] == "2026-09-25"
    artifact = Path(first.raw_artifacts[0].artifact_reference.removeprefix("file://"))
    assert artifact.read_bytes() == provider.raw
    assert (artifact.parent / "manifest.json").is_file()


def test_official_response_reordering_does_not_create_a_false_release(tmp_path):
    rows = _rows()
    first = worker.run_cbr_warning_handler(
        FakeContext(tmp_path / "first"), provider=FakeProvider(rows)
    )
    second = worker.run_cbr_warning_handler(
        FakeContext(tmp_path / "second"), provider=FakeProvider(list(reversed(rows)))
    )

    assert (
        first.raw_artifacts[0].checksum
        != second.raw_artifacts[0].checksum
    )
    assert (
        first.checksum_metadata["normalized_sha256"]
        == second.checksum_metadata["normalized_sha256"]
    )
    assert (
        first.checksum_metadata["release_identity"]
        == second.checksum_metadata["release_identity"]
    )


def test_daily_schedule_is_idempotent_and_uses_existing_worker_foundation(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        worker,
        "create_job",
        lambda _session, **kwargs: calls.append(kwargs) or kwargs,
    )
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)
    session = SimpleNamespace(get=lambda *_args: approval)

    first = worker.schedule_cbr_warning_check(
        session, raw_root=tmp_path, now=NOW
    )
    second = worker.schedule_cbr_warning_check(
        session, raw_root=tmp_path, now=NOW
    )

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["source_id"] == "cbr_warning_list"
    assert first["schedule_metadata"]["check_frequency"] == "daily"
    assert scheduler.HANDLERS["cbr_warning_list"] is scheduler._enqueue_cbr_warning
    assert len(calls) == 2


def test_scheduler_preserves_all_operational_sources_and_priority(monkeypatch):
    expected = {
        "fns_egrul",
        "fns_egrip",
        "fns_tax_offence",
        "fns_revenue_expenses",
        "fns_tax_debt",
        "fns_tax_paid",
        "cbr_warning_list",
        "fns_headcount",
        "fns_msp",
        "fns_tax_regime",
        "fns_sme_support",
        "fns_disqualified",
        "mintrans_ted_registry",
        "girbo_accounting",
        "erknm_inspections",
        "cbr_finorg",
        "roszdrav_pharma_licenses",
        "roszdrav_narcotics_licenses",
        "roszdrav_medical_device_maintenance_licenses",
        "firmoteka",
        "fns_npd",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "rkn_personal_data_operators",
        "eis_rnp",
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
        "rkn_information_distributors",
        "rkn_hosting_providers",
    }
    assert set(scheduler.HANDLERS) == expected
    assert scheduler.FNS_BULK_DATASET_CODES == expected - {
        "cbr_warning_list",
        "erknm_inspections",
        "cbr_finorg",
        *scheduler.ROSZDRAV_LICENSE_DATASET_CODES,
        "mintrans_ted_registry",
        "girbo_accounting",
        "firmoteka",
        "fns_npd",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "rkn_personal_data_operators",
        "eis_rnp",
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
        "rkn_information_distributors",
        "rkn_hosting_providers",
    }
    assert scheduler.SCHEDULED_SOURCE_DATASET_CODES == expected
    calls = []
    monkeypatch.setattr(
        scheduler,
        "HANDLERS",
        {source_id: lambda source_id=source_id: calls.append(source_id)
         for source_id in expected},
    )

    result = scheduler.run_due_updates(due_codes=reversed(sorted(expected)))

    assert calls == [
        "firmoteka",
        "fns_egrul",
        "fns_egrip",
        "fns_tax_offence",
        "fns_revenue_expenses",
        "fns_tax_debt",
        "fns_tax_paid",
        "cbr_warning_list",
        "fns_headcount",
        "fns_msp",
        "fns_tax_regime",
        "fns_sme_support",
        "fns_disqualified",
        "erknm_inspections",
        "cbr_finorg",
        "roszdrav_pharma_licenses",
        "roszdrav_narcotics_licenses",
        "roszdrav_medical_device_maintenance_licenses",
        "mintrans_ted_registry",
        "girbo_accounting",
        "fns_npd",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "eis_rnp",
        "rkn_personal_data_operators",
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
        "rkn_information_distributors",
        "rkn_hosting_providers",
    ]
    assert set(result) == expected
    assert set(result.values()) == {"success"}


def test_postgresql_publish_then_unchanged_check_preserves_generation(
    cbr_db, tmp_path, monkeypatch
):
    suffix = str(uuid4().int % 10**8).zfill(8)
    inn = f"77{suffix}"
    provider = FakeProvider(_rows(inn))
    monkeypatch.setattr(worker, "utc_now", lambda: NOW)
    first = worker.run_cbr_warning_handler(
        FakeContext(tmp_path), provider=provider
    )

    with cbr_db() as session:
        source = session.scalar(
            sa.select(DataSource).where(DataSource.code == "cbr")
        )
        if source is None:
            source = DataSource(
                code="cbr",
                name="Банк России",
                source_type="official",
                priority=10,
                enabled=True,
            )
            session.add(source)
            session.flush()
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == worker.DATASET_CODE)
        )
        if dataset is None:
            dataset = DataSet(
                source_id=source.id,
                code=worker.DATASET_CODE,
                name="CBR Warning List",
                domain="financial_warning",
                update_mode="bulk",
                data_format="json",
                priority=10,
                enabled=False,
            )
            session.add(dataset)
            session.flush()
        session.execute(
            sa.delete(CbrWarningListEntry).where(
                CbrWarningListEntry.dataset_id == dataset.id
            )
        )
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == worker.SOURCE_ID
            )
        )
        company = Company(
            inn=inn,
            name="CBR exact match",
            entity_type="legal",
        )
        session.add(company)
        session.flush()

        published = worker.publish_cbr_warning_worker_result(
            session, SimpleNamespace(schedule_metadata={}), first
        )
        assert published.counters.records_published == 2
        assert dataset.record_count == 2
        assert dataset.coverage["matched"] == 1
        assert dataset.coverage["risk_summary_candidate_companies"] == 1
        assert dataset.last_data_date == date(2026, 9, 24)
        assert dataset.official_actual_until == date(2026, 9, 24)

        state = WorkerPublicationState(
            source_id=worker.SOURCE_ID,
            active_pointer=published.staging_result.staging_pointer,
            generation=1,
            last_fencing_token=1,
            validation_metadata={
                "checksum": published.staging_result.checksum,
                "validation": published.staging_result.validation.metadata,
                "staging": published.staging_result.metadata,
            },
        )
        session.add(state)
        session.flush()
        last_success = dataset.last_success_at

        next_day = NOW + timedelta(days=1)
        second = worker.run_cbr_warning_handler(
            FakeContext(tmp_path, discovered_at=next_day), provider=provider
        )
        monkeypatch.setattr(worker, "utc_now", lambda: next_day)
        checked = worker.publish_cbr_warning_worker_result(
            session, SimpleNamespace(schedule_metadata={}), second
        )
        session.flush()

        assert checked.staging_result is None
        assert checked.counters.records_published == 0
        assert dataset.last_success_at == last_success
        assert dataset.checked_at == next_day
        assert dataset.official_actual_until == next_day.date()
        assert session.get(WorkerPublicationState, worker.SOURCE_ID).generation == 1
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(CbrWarningListEntry)
            .where(CbrWarningListEntry.dataset_id == dataset.id)
        ) == 2
