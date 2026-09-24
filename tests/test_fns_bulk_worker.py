import csv
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.database.postgres import engine
from app.ingestion import fns_bulk_worker as bulk
from app.ingestion import fns_revenue_expense as revexp
from app.ingestion import fns_tax_offence as taxoffence
from app.ingestion import fns_tax_payment as taxpayment
from app.models.company import Company
from app.models.revenue_expense import CompanyRevenueExpenseSnapshot
from app.models.source import DataSet, DataSource
from app.models.tax_offence import CompanyTaxOffence
from app.models.tax_payment import CompanyTaxPaymentItem, CompanyTaxPaymentSnapshot
from app.models.worker import WorkerPublicationState
from app.services import data_readiness_scheduler as scheduler
from app.worker.contracts import (
    ExecutionCounters,
    HandlerResult,
    StagingResult,
    ValidationResult,
)


NOW = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


@pytest.fixture
def bulk_db():
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


def _release(spec):
    return bulk.FnsRelease(
        source_page_url=spec.source_page_url,
        artifact_url=f"https://data.nalog.ru/opendata/{spec.source_path}/data-20260901-structure-20200101.zip",
        xsd_url=f"https://data.nalog.ru/opendata/{spec.source_path}/structure-20200101.xsd",
        source_data_date=date(2025, 12, 31),
        actual_until=date(2027, 1, 1),
        discovered_at=NOW,
        provenance="Данные за 2025 год",
    )


def test_official_discovery_extracts_current_zip_xsd_and_separate_source_date():
    spec = taxoffence._worker_spec()
    html = f"""
      <a href="https://data.nalog.ru/opendata/{spec.source_path}/data-20251201-structure-20191201.zip">ZIP</a>
      <a href="https://data.nalog.ru/opendata/{spec.source_path}/structure-20181201.xsd">XSD</a>
      <td property="dc:provenance">Данные на 31.12.2024</td>
      <td property="dc:valid" content="01.12.2026">01.12.2026</td>
    """.encode()

    release = bulk.discover_fns_release(spec, now=NOW, fetch=lambda _url: (html, {}))

    assert release.source_data_date == date(2024, 12, 31)
    assert release.actual_until == date(2026, 12, 1)
    assert release.artifact_url.endswith("data-20251201-structure-20191201.zip")
    assert release.source_data_date.isoformat() not in release.artifact_url


def test_paytax_parser_preserves_items_and_computes_totals():
    document = taxpayment.ET.fromstring(
        """
        <Документ ИдДок="paytax-doc-1" ДатаДок="01.04.2026" ДатаСост="31.12.2025">
          <СведНП ИННЮЛ="7701234567" НаимОрг="ООО Тест" />
          <СвУплСумНал НаимНалог="Налог на прибыль" СумУплНал="1000.00" />
          <СвУплСумНал НаимНалог="Суммы пеней" СумУплНал="25.50" />
        </Документ>
        """
    )

    record = taxpayment.parse_tax_payment_document(document)

    assert record["inn"] == "7701234567"
    assert record["data_date"] == date(2025, 12, 31)
    assert record["total_amount"] == taxpayment.Decimal("1025.50")
    assert record["tax_amount"] == taxpayment.Decimal("1000.00")
    assert record["penalty_amount"] == taxpayment.Decimal("25.50")
    assert record["stored_item_count"] == 2
    assert {item["payment_type"] for item in record["items"]} == {"tax", "penalty"}


def test_fns_bulk_sources_are_separate_and_handlers_are_non_empty():
    s03 = revexp._worker_spec()
    s04 = taxoffence._worker_spec()
    paytax = taxpayment._worker_spec()

    assert s03.source_id == "fns_revenue_expenses"
    assert s04.source_id == "fns_tax_offence"
    assert paytax.source_id == "fns_tax_paid"
    assert len({s03.source_id, s04.source_id, paytax.source_id}) == 3
    assert len({s03.handler_version, s04.handler_version, paytax.handler_version}) == 3
    assert scheduler.HANDLERS[s03.dataset_code] is scheduler._enqueue_revenue_expense
    assert scheduler.HANDLERS[s04.dataset_code] is scheduler._enqueue_tax_offence
    assert scheduler.HANDLERS[paytax.dataset_code] is scheduler._enqueue_tax_payment
    assert paytax.check_frequency == "weekly"
    assert paytax.check_interval.days == 7


def test_scheduler_executes_bulk_sources_in_priority_order(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler,
        "HANDLERS",
        {
            "fns_tax_offence": lambda: calls.append("S04"),
            "fns_revenue_expenses": lambda: calls.append("S03"),
            "fns_tax_paid": lambda: calls.append("PAYTAX"),
        },
    )

    result = scheduler.run_due_updates(
        due_codes=["fns_tax_paid", "fns_revenue_expenses", "fns_tax_offence"]
    )

    assert calls == ["S04", "S03", "PAYTAX"]
    assert result == {
        "fns_tax_offence": "success",
        "fns_revenue_expenses": "success",
        "fns_tax_paid": "success",
    }


def test_source_scoped_activation_enables_s04_without_enabling_s03(monkeypatch):
    s04 = SimpleNamespace(
        code="fns_tax_offence",
        enabled=False,
        auto_update_status=AutoUpdateStatus.NOT_CONFIGURED,
        next_expected_update_at=None,
        last_success_at=None,
        operational_status=OperationalStatus.NOT_CONFIGURED,
    )
    s03 = SimpleNamespace(
        code="fns_revenue_expenses",
        enabled=False,
        auto_update_status=AutoUpdateStatus.NOT_CONFIGURED,
        next_expected_update_at=None,
        last_success_at=None,
        operational_status=OperationalStatus.NOT_CONFIGURED,
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalars(self, _statement):
            return [s04]

        def commit(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)

    scheduler.configure_fns_bulk_schedules(
        enabled=True,
        dataset_codes=["fns_tax_offence"],
        now=NOW,
    )

    assert s04.enabled is True
    assert s04.auto_update_status == AutoUpdateStatus.CONFIGURED
    assert s04.next_expected_update_at == NOW
    assert s03.enabled is False
    assert s03.auto_update_status == AutoUpdateStatus.NOT_CONFIGURED
    assert s03.next_expected_update_at is None


def test_check_only_worker_records_run_without_downloading(monkeypatch):
    spec = taxoffence._worker_spec()
    release = _release(spec)
    monkeypatch.setattr(bulk, "stage_release", lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")))
    context = SimpleNamespace(schedule_metadata={**release.as_metadata(), "check_only": True})

    result = bulk.run_bulk_handler(context, spec=spec, iterator=taxoffence.iter_xml_records)

    assert result.raw_artifacts == ()
    assert result.staging_result is None
    assert result.checksum_metadata == {
        "release_identity": release.identity,
        "check_only": True,
        "replay_snapshot": False,
    }


def test_release_enqueue_is_idempotent_and_source_scoped(monkeypatch, tmp_path):
    calls = []

    def fake_create_job(_session, **kwargs):
        calls.append(kwargs)
        return kwargs

    monkeypatch.setattr(bulk, "create_job", fake_create_job)
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)
    session = SimpleNamespace(get=lambda *_args: approval)
    s04 = taxoffence._worker_spec()
    release = _release(s04)

    first = bulk.enqueue_bulk_release(session, spec=s04, release=release, raw_root=tmp_path)
    second = bulk.enqueue_bulk_release(session, spec=s04, release=release, raw_root=tmp_path)

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["source_id"] == "fns_tax_offence"
    assert first["schedule_metadata"]["source_data_date"] == "2025-12-31"
    assert len(calls) == 2


def test_same_release_becomes_daily_check_job_not_publication(monkeypatch, tmp_path):
    spec = revexp._worker_spec()
    release = _release(spec)
    state = SimpleNamespace(
        active_pointer="file:///accepted/normalized.jsonl",
        validation_metadata={
            "checksum": "a" * 64,
            "validation": {"release_identity": release.identity},
        },
    )
    session = SimpleNamespace(get=lambda *_args: state)
    captured = {}
    monkeypatch.setattr(bulk, "discover_fns_release", lambda *_args, **_kwargs: release)

    def fake_enqueue(_session, **kwargs):
        captured.update(kwargs)
        return "check-job"

    monkeypatch.setattr(bulk, "enqueue_bulk_release", fake_enqueue)

    assert bulk.schedule_source_check(session, spec=spec, raw_root=tmp_path, now=NOW) == "check-job"
    assert captured["check_only"] is True
    assert captured["replay_pointer"] == state.active_pointer
    assert captured["replay_checksum"] == "a" * 64
    assert captured["scheduled_for"] == NOW.date()


def test_scheduler_failure_signal_preserves_last_success(monkeypatch):
    last_success = datetime(2026, 8, 25, tzinfo=timezone.utc)
    dataset = SimpleNamespace(
        checked_at=None,
        last_error=None,
        last_error_at=None,
        operational_status="current",
        retry_count=0,
        next_retry_at=None,
        last_success_at=last_success,
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalar(self, _statement):
            return dataset

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)

    scheduler._record_schedule_failure(
        "fns_revenue_expenses", RuntimeError("network down token=secret")
    )

    assert dataset.last_success_at == last_success
    assert dataset.operational_status == "error"
    assert dataset.last_error == "network down token=[REDACTED]"
    assert dataset.retry_count == 1
    assert dataset.next_retry_at > dataset.last_error_at


def test_immutable_manifest_is_retry_stable(monkeypatch, tmp_path):
    spec = taxoffence._worker_spec()
    release = _release(spec)
    sequence = 0

    def fake_download(url, root):
        nonlocal sequence
        sequence += 1
        path = root / f"download-{sequence}"
        path.write_bytes(b"zip-bytes" if url.endswith(".zip") else b"xsd-bytes")
        return path, {"Content-Length": str(path.stat().st_size)}

    monkeypatch.setattr(bulk, "_download_temp", fake_download)

    first = bulk.stage_release(spec, release, raw_root=tmp_path)
    second = bulk.stage_release(spec, release, raw_root=tmp_path)

    assert first[2] == second[2]
    assert first[2]["retrieved_at"] == NOW.isoformat()
    assert first[2]["artifact_reference"].startswith("file:")
    assert first[2]["xsd_reference"].startswith("file:")


def test_paytax_capacity_prestage_is_reused_by_later_real_job(
    monkeypatch, tmp_path
):
    spec = taxpayment._worker_spec()
    capacity_release = _release(spec)
    real_job_release = bulk.FnsRelease(
        source_page_url=capacity_release.source_page_url,
        artifact_url=capacity_release.artifact_url,
        xsd_url=capacity_release.xsd_url,
        source_data_date=capacity_release.source_data_date,
        actual_until=capacity_release.actual_until,
        discovered_at=NOW.replace(minute=NOW.minute + 5),
        provenance=capacity_release.provenance,
    )
    sequence = 0

    def fake_download(url, root):
        nonlocal sequence
        sequence += 1
        path = root / f"download-{sequence}"
        path.write_bytes(b"zip-bytes" if url.endswith(".zip") else b"xsd-bytes")
        return path, {"Content-Length": str(path.stat().st_size)}

    monkeypatch.setattr(bulk, "_download_temp", fake_download)

    capacity = bulk.stage_release(spec, capacity_release, raw_root=tmp_path)
    manifest_path = capacity[0].parent / "manifest.json"
    first_bytes = manifest_path.read_bytes()
    real_job = bulk.stage_release(spec, real_job_release, raw_root=tmp_path)

    assert real_job[2] == capacity[2]
    assert real_job[2]["discovered_at"] == NOW.isoformat()
    assert real_job[2]["retrieved_at"] == NOW.isoformat()
    assert manifest_path.read_bytes() == first_bytes


def test_existing_bulk_manifest_rejects_xsd_invariant_change(
    monkeypatch, tmp_path
):
    spec = taxpayment._worker_spec()
    release = _release(spec)
    sequence = 0

    def fake_download(url, root):
        nonlocal sequence
        sequence += 1
        path = root / f"download-{sequence}"
        path.write_bytes(b"zip-bytes" if url.endswith(".zip") else b"xsd-bytes")
        return path, {"Content-Length": str(path.stat().st_size)}

    monkeypatch.setattr(bulk, "_download_temp", fake_download)
    first = bulk.stage_release(spec, release, raw_root=tmp_path)
    manifest_path = first[0].parent / "manifest.json"
    first_bytes = manifest_path.read_bytes()
    changed = bulk.FnsRelease(
        source_page_url=release.source_page_url,
        artifact_url=release.artifact_url,
        xsd_url=release.xsd_url.replace("structure-20200101", "structure-20200102"),
        source_data_date=release.source_data_date,
        actual_until=release.actual_until,
        discovered_at=NOW.replace(minute=NOW.minute + 5),
        provenance=release.provenance,
    )

    with pytest.raises(bulk.InvalidDataError, match="manifest invariant differs"):
        bulk.stage_release(spec, changed, raw_root=tmp_path)

    assert manifest_path.read_bytes() == first_bytes


def test_old_worker_failure_is_not_replayed_after_newer_success(monkeypatch):
    finished = datetime(2026, 9, 23, tzinfo=timezone.utc)
    dataset = SimpleNamespace(
        last_success_at=NOW,
        last_error_at=None,
        last_error=None,
        retry_count=0,
        next_retry_at=None,
        operational_status="current",
    )
    run = SimpleNamespace(
        status="failed",
        finished_at=finished,
        errors=[{"message": "old failure"}],
    )
    job = SimpleNamespace(source_id="fns_tax_offence", status="failed", next_attempt_at=None)

    class Result:
        def all(self):
            return [(run, job)]

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result()

        def scalar(self, _statement):
            return dataset

        def commit(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)

    assert scheduler.sync_worker_failure_signals() == 0
    assert dataset.last_error is None
    assert dataset.operational_status == "current"


def test_newer_successful_check_suppresses_failure_without_moving_last_success(
    monkeypatch,
):
    last_success = NOW - timedelta(days=30)
    failed_at = NOW - timedelta(minutes=5)
    checked_at = NOW
    next_check = NOW + timedelta(days=7)
    dataset = SimpleNamespace(
        last_success_at=last_success,
        last_error_at=failed_at,
        last_error="superseded check failure",
        retry_count=1,
        next_retry_at=NOW + timedelta(minutes=10),
        operational_status="error",
        official_actual_until=date.max,
        checked_at=checked_at,
        next_expected_update_at=next_check,
    )
    failed_run = SimpleNamespace(
        status="failed",
        finished_at=failed_at,
        errors=[{"message": "superseded check failure"}],
    )
    failed_job = SimpleNamespace(
        source_id="fns_tax_paid", status="failed", next_attempt_at=None
    )
    successful_check = SimpleNamespace(status="succeeded", finished_at=checked_at)
    successful_job = SimpleNamespace(source_id="fns_tax_paid")

    class Result:
        def all(self):
            return [(successful_check, successful_job), (failed_run, failed_job)]

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result()

        def scalar(self, _statement):
            return dataset

        def commit(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)

    assert scheduler.sync_worker_failure_signals() == 1
    assert dataset.last_success_at == last_success
    assert dataset.last_error is None
    assert dataset.last_error_at is None
    assert dataset.retry_count == 0
    assert dataset.next_retry_at is None
    assert dataset.operational_status == "current"
    assert dataset.checked_at == checked_at
    assert dataset.next_expected_update_at == next_check


def test_newer_successful_check_recovers_stale_when_release_expired(monkeypatch):
    failed_at = NOW - timedelta(minutes=5)
    dataset = SimpleNamespace(
        last_success_at=NOW - timedelta(days=30),
        last_error_at=failed_at,
        last_error="superseded check failure",
        retry_count=1,
        next_retry_at=NOW + timedelta(minutes=10),
        operational_status="error",
        official_actual_until=date.min,
    )
    successful_check = SimpleNamespace(status="succeeded", finished_at=NOW)
    successful_job = SimpleNamespace(source_id="fns_tax_paid")

    class Result:
        def all(self):
            return [(successful_check, successful_job)]

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result()

        def scalar(self, _statement):
            return dataset

        def commit(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)

    assert scheduler.sync_worker_failure_signals() == 1
    assert dataset.last_error is None
    assert dataset.last_error_at is None
    assert dataset.retry_count == 0
    assert dataset.next_retry_at is None
    assert dataset.operational_status == "stale"


def test_latest_worker_failure_is_projected_after_older_success(monkeypatch):
    last_success = NOW - timedelta(days=30)
    succeeded_at = NOW - timedelta(minutes=10)
    failed_at = NOW - timedelta(minutes=5)
    dataset = SimpleNamespace(
        last_success_at=last_success,
        last_error_at=None,
        last_error=None,
        retry_count=0,
        next_retry_at=None,
        operational_status="current",
    )
    failed_run = SimpleNamespace(
        status="failed",
        finished_at=failed_at,
        errors=[{"message": "latest check failure"}],
    )
    failed_job = SimpleNamespace(
        source_id="fns_tax_paid",
        status="retry_scheduled",
        next_attempt_at=NOW + timedelta(minutes=15),
    )
    old_success = SimpleNamespace(status="succeeded", finished_at=succeeded_at)
    successful_job = SimpleNamespace(source_id="fns_tax_paid")

    class Result:
        def all(self):
            return [(failed_run, failed_job), (old_success, successful_job)]

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result()

        def scalar(self, _statement):
            return dataset

        def commit(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)

    assert scheduler.sync_worker_failure_signals() == 1
    assert dataset.last_success_at == last_success
    assert dataset.last_error == "latest check failure"
    assert dataset.last_error_at == failed_at
    assert dataset.retry_count == 1
    assert dataset.next_retry_at == failed_job.next_attempt_at
    assert dataset.operational_status == "error"


def test_sources_have_independent_release_idempotency_namespaces(monkeypatch, tmp_path):
    keys = []
    monkeypatch.setattr(
        bulk,
        "create_job",
        lambda _session, **kwargs: keys.append(kwargs["idempotency_key"]) or kwargs,
    )
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)
    session = SimpleNamespace(get=lambda *_args: approval)
    for spec in (
        taxoffence._worker_spec(),
        revexp._worker_spec(),
        taxpayment._worker_spec(),
    ):
        bulk.enqueue_bulk_release(session, spec=spec, release=_release(spec), raw_root=Path(tmp_path))

    assert keys[0].startswith("fns_tax_offence:release:")
    assert keys[1].startswith("fns_revenue_expenses:release:")
    assert keys[2].startswith("fns_tax_paid:release:")
    assert len(set(keys)) == 3


def test_machine_readable_queue_has_required_columns_and_unique_processes():
    path = Path(__file__).parents[1] / "docs" / "source_execution_queue.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert rows
    assert set(rows[0]) == {
        "source_id",
        "owner",
        "access_channel",
        "card_fact",
        "existing_code",
        "publication_frequency",
        "check_frequency",
        "next_executable_action",
    }
    source_ids = [row["source_id"] for row in rows]
    assert len(source_ids) == len(set(source_ids))


def _seed_accepted_snapshot(
    factory,
    tmp_path,
    *,
    spec,
    release,
):
    suffix = str(uuid4().int % 10**8).zfill(8)
    first_inn = f"77{suffix}"
    second_inn = f"78{suffix}"
    if spec.kind == "tax_offence":
        rows = (
            {
                "inn": first_inn,
                "data_date": release.source_data_date.isoformat(),
                "document_date": "2025-12-20",
                "document_id": f"doc-first-{suffix}",
                "fine_amount": "100.00",
            },
            {
                "inn": second_inn,
                "data_date": release.source_data_date.isoformat(),
                "document_date": "2025-12-21",
                "document_id": f"doc-second-{suffix}",
                "fine_amount": "200.00",
            },
        )
        model = CompanyTaxOffence
    elif spec.kind == "revenue_expense":
        rows = (
            {
                "inn": first_inn,
                "company_name": f"First {suffix}",
                "data_date": release.source_data_date.isoformat(),
                "data_year": release.source_data_date.year,
                "document_date": "2025-12-20",
                "document_id": f"doc-first-{suffix}",
                "revenue": "1000.00",
                "expenses": "700.00",
                "profit_loss": "300.00",
            },
            {
                "inn": second_inn,
                "company_name": f"Second {suffix}",
                "data_date": release.source_data_date.isoformat(),
                "data_year": release.source_data_date.year,
                "document_date": "2025-12-21",
                "document_id": f"doc-second-{suffix}",
                "revenue": "2000.00",
                "expenses": "1200.00",
                "profit_loss": "800.00",
            },
        )
        model = CompanyRevenueExpenseSnapshot
    else:
        rows = (
            {
                "inn": first_inn,
                "company_name": f"First {suffix}",
                "data_date": release.source_data_date.isoformat(),
                "data_year": release.source_data_date.year,
                "document_date": "2025-12-20",
                "document_id": f"doc-first-{suffix}",
                "total_amount": "1250.00",
                "tax_amount": "1000.00",
                "insurance_amount": "250.00",
                "penalty_amount": "0.00",
                "non_tax_amount": "0.00",
                "other_amount": "0.00",
                "source_item_count": 2,
                "stored_item_count": 2,
                "items": [
                    {"tax_name": "Налог", "payment_type": "tax", "amount": "1000.00"},
                    {"tax_name": "Взносы", "payment_type": "insurance", "amount": "250.00"},
                ],
            },
            {
                "inn": second_inn,
                "company_name": f"Second {suffix}",
                "data_date": release.source_data_date.isoformat(),
                "data_year": release.source_data_date.year,
                "document_date": "2025-12-21",
                "document_id": f"doc-second-{suffix}",
                "total_amount": "2200.00",
                "tax_amount": "2000.00",
                "insurance_amount": "0.00",
                "penalty_amount": "200.00",
                "non_tax_amount": "0.00",
                "other_amount": "0.00",
                "source_item_count": 2,
                "stored_item_count": 2,
                "items": [
                    {"tax_name": "Налог", "payment_type": "tax", "amount": "2000.00"},
                    {"tax_name": "Суммы пеней", "payment_type": "penalty", "amount": "200.00"},
                ],
            },
        )
        model = CompanyTaxPaymentSnapshot
    snapshot = tmp_path / "accepted-normalized.jsonl"
    snapshot.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    checksum, _size = bulk._hash_file(snapshot)
    result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer=snapshot.as_uri(),
            checksum=checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={
                    "release_identity": release.identity,
                    "source_data_date": release.source_data_date.isoformat(),
                },
            ),
            metadata={"source_id": spec.source_id},
        ),
        counters=ExecutionCounters(records_seen=2, records_written=2),
    )
    claim = SimpleNamespace(schedule_metadata=release.as_metadata())

    with factory() as session:
        source = session.scalar(sa.select(DataSource).where(DataSource.code == "fns"))
        if source is None:
            source = DataSource(
                code="fns",
                name="FNS",
                source_type="official",
                priority=10,
                enabled=True,
            )
            session.add(source)
            session.flush()
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == spec.dataset_code)
        )
        if dataset is None:
            dataset = DataSet(
                source_id=source.id,
                code=spec.dataset_code,
                name=f"FNS {spec.kind}",
                domain=spec.kind,
                update_mode="bulk",
                data_format="xml",
                priority=10,
                enabled=False,
            )
            session.add(dataset)
            session.flush()
        else:
            session.execute(
                sa.delete(model).where(
                    model.dataset_id == dataset.id
                )
            )
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == spec.source_id
            )
        )
        first = Company(
            inn=first_inn,
            name=f"Replay first {suffix}",
            entity_type="legal",
        )
        session.add(first)
        session.flush()
        published = bulk.publish_bulk_result(
            session,
            claim,
            result,
            spec=spec,
        )
        state = WorkerPublicationState(
            source_id=spec.source_id,
            active_pointer=snapshot.as_uri(),
            generation=1,
            last_fencing_token=1,
            validation_metadata={
                "checksum": checksum,
                "validation": published.staging_result.validation.metadata,
                "staging": published.staging_result.metadata,
            },
        )
        session.add(state)
        session.commit()
        return {
            "spec": spec,
            "dataset_id": dataset.id,
            "first_company_id": first.id,
            "second_inn": second_inn,
            "pointer": snapshot.as_uri(),
            "checksum": checksum,
            "release": release,
            "model": model,
        }


def _replay_claim(seed):
    return SimpleNamespace(
        schedule_metadata={
            **seed["release"].as_metadata(),
            "check_only": True,
            "replay_snapshot": True,
            "replay_pointer": seed["pointer"],
            "replay_checksum": seed["checksum"],
        }
    )


def test_postgresql_freshness_boundary_and_next_day_do_not_republish(
    bulk_db,
    tmp_path,
    monkeypatch,
):
    release = _release(taxoffence._worker_spec())
    boundary = datetime(2027, 1, 1, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(bulk, "utc_now", lambda: boundary)
    spec = taxoffence._worker_spec()
    seed = _seed_accepted_snapshot(
        bulk_db,
        tmp_path,
        spec=spec,
        release=release,
    )

    with bulk_db() as session:
        dataset = session.get(DataSet, seed["dataset_id"])
        state = session.get(WorkerPublicationState, seed["spec"].source_id)
        last_success = dataset.last_success_at
        published_at = dataset.published_at
        assert dataset.official_actual_until == release.actual_until
        assert dataset.operational_status == "current"
        assert state.generation == 1

        bulk.publish_bulk_result(
            session,
            _replay_claim(seed),
            HandlerResult(),
            spec=seed["spec"],
        )
        session.flush()
        assert dataset.checked_at == boundary
        assert dataset.operational_status == "current"
        assert dataset.last_success_at == last_success
        assert dataset.published_at == published_at
        assert state.generation == 1

        next_day = datetime(2027, 1, 2, tzinfo=timezone.utc)
        monkeypatch.setattr(bulk, "utc_now", lambda: next_day)
        bulk.publish_bulk_result(
            session,
            _replay_claim(seed),
            HandlerResult(),
            spec=seed["spec"],
        )
        session.flush()
        assert dataset.checked_at == next_day
        assert dataset.official_actual_until == release.actual_until
        assert dataset.operational_status == "stale"
        assert dataset.last_success_at == last_success
        assert dataset.published_at == published_at
        assert state.generation == 1


@pytest.mark.parametrize(
    "source_kind",
    ("tax_offence", "revenue_expense", "tax_payment"),
)
def test_postgresql_same_release_replay_projects_new_master_company_idempotently(
    bulk_db,
    tmp_path,
    monkeypatch,
    source_kind,
):
    monkeypatch.setattr(bulk, "utc_now", lambda: NOW)
    spec = (
        taxoffence._worker_spec()
        if source_kind == "tax_offence"
        else (
            revexp._worker_spec()
            if source_kind == "revenue_expense"
            else taxpayment._worker_spec()
        )
    )
    seed = _seed_accepted_snapshot(
        bulk_db,
        tmp_path,
        spec=spec,
        release=_release(spec),
    )
    model = seed["model"]

    with bulk_db() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(model).where(
                model.dataset_id == seed["dataset_id"]
            )
        ) == 1
        second = Company(
            inn=seed["second_inn"],
            name="Replay second company",
            entity_type="legal",
        )
        session.add(second)
        session.flush()

        first_replay = bulk.publish_bulk_result(
            session,
            _replay_claim(seed),
            HandlerResult(),
            spec=seed["spec"],
        )
        session.flush()
        assert session.scalar(
            sa.select(sa.func.count()).select_from(model).where(
                model.dataset_id == seed["dataset_id"]
            )
        ) == 2
        assert session.scalar(
            sa.select(sa.func.count()).select_from(model).where(
                model.company_id == second.id,
                model.dataset_id == seed["dataset_id"],
            )
        ) == 1
        assert first_replay.counters.records_published == 1

        second_replay = bulk.publish_bulk_result(
            session,
            _replay_claim(seed),
            HandlerResult(),
            spec=seed["spec"],
        )
        session.flush()
        assert session.scalar(
            sa.select(sa.func.count()).select_from(model).where(
                model.dataset_id == seed["dataset_id"]
            )
        ) == 2
        assert second_replay.counters.records_published == 0
        assert session.get(
            WorkerPublicationState, seed["spec"].source_id
        ).generation == 1
        if source_kind == "tax_payment":
            assert session.scalar(
                sa.select(sa.func.count()).select_from(CompanyTaxPaymentItem)
            ) == 4
            assert session.get(DataSet, seed["dataset_id"]).next_expected_update_at == (
                NOW + taxpayment.timedelta(days=7)
            )
