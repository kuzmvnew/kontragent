import csv
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.ingestion import fns_bulk_worker as bulk
from app.ingestion import fns_revenue_expense as revexp
from app.ingestion import fns_tax_offence as taxoffence
from app.services import data_readiness_scheduler as scheduler


NOW = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


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


def test_s03_and_s04_are_separate_operational_sources_and_handlers_are_non_empty():
    s03 = revexp._worker_spec()
    s04 = taxoffence._worker_spec()

    assert s03.source_id == "fns_revenue_expenses"
    assert s04.source_id == "fns_tax_offence"
    assert s03.source_id != s04.source_id
    assert s03.handler_version != s04.handler_version
    assert scheduler.HANDLERS[s03.dataset_code] is scheduler._enqueue_revenue_expense
    assert scheduler.HANDLERS[s04.dataset_code] is scheduler._enqueue_tax_offence


def test_scheduler_executes_s04_before_s03(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler,
        "HANDLERS",
        {
            "fns_tax_offence": lambda: calls.append("S04"),
            "fns_revenue_expenses": lambda: calls.append("S03"),
        },
    )

    result = scheduler.run_due_updates(
        due_codes=["fns_revenue_expenses", "fns_tax_offence"]
    )

    assert calls == ["S04", "S03"]
    assert result == {"fns_tax_offence": "success", "fns_revenue_expenses": "success"}


def test_check_only_worker_records_run_without_downloading(monkeypatch):
    spec = taxoffence._worker_spec()
    release = _release(spec)
    monkeypatch.setattr(bulk, "stage_release", lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")))
    context = SimpleNamespace(schedule_metadata={**release.as_metadata(), "check_only": True})

    result = bulk.run_bulk_handler(context, spec=spec, iterator=taxoffence.iter_xml_records)

    assert result.raw_artifacts == ()
    assert result.staging_result is None
    assert result.checksum_metadata == {"release_identity": release.identity, "check_only": True}


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
        validation_metadata={"validation": {"release_identity": release.identity}}
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


def test_two_sources_have_independent_release_idempotency_namespaces(monkeypatch, tmp_path):
    keys = []
    monkeypatch.setattr(
        bulk,
        "create_job",
        lambda _session, **kwargs: keys.append(kwargs["idempotency_key"]) or kwargs,
    )
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)
    session = SimpleNamespace(get=lambda *_args: approval)
    for spec in (taxoffence._worker_spec(), revexp._worker_spec()):
        bulk.enqueue_bulk_release(session, spec=spec, release=_release(spec), raw_root=Path(tmp_path))

    assert keys[0].startswith("fns_tax_offence:release:")
    assert keys[1].startswith("fns_revenue_expenses:release:")
    assert keys[0] != keys[1]


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
