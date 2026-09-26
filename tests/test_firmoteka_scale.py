from datetime import datetime, timedelta, timezone
import json
from threading import Barrier, Lock, local

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import firmoteka_worker as worker
from app.models.firmoteka import (
    FirmotekaCatalogPage,
    FirmotekaCompanySnapshot,
    FirmotekaCrawlItem,
    FirmotekaCrawlRun,
    FirmotekaRawArtifact,
)
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerJob
from app.services.source_factory_registry_service import ensure_source_factory_datasets


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


def _factory():
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return connection, transaction, factory


def _legal_inn(prefix: int) -> str:
    first_nine = f"{prefix:09d}"
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    check = sum(int(value) * weight for value, weight in zip(first_nine, weights))
    return first_nine + str(check % 11 % 10)


def _crawl(**overrides):
    values = {
        "run_kind": "initial",
        "status": "running",
        "phase": "catalog",
        "cursor": {},
        "request_delay_seconds": 4,
        "concurrency": 2,
        "catalog_concurrency": 2,
        "company_concurrency": 2,
        "backpressure_threshold": 3,
        "daily_refresh_horizon_days": 30,
        "daily_refresh_budget": 500,
        "lane_request_state": {},
        "started_at": NOW,
    }
    values.update(overrides)
    return FirmotekaCrawlRun(**values)


def test_parallel_lanes_apply_four_second_gap_independently(monkeypatch):
    monkeypatch.delenv("FIRMOTEKA_EGRESS_POOL_JSON", raising=False)

    class FakeClock:
        def __init__(self):
            self.local = local()

        def monotonic(self):
            return getattr(self.local, "value", 0.0)

        def sleep(self, seconds):
            self.local.value = self.monotonic() + seconds

        def now(self):
            return NOW + timedelta(seconds=self.monotonic())

    clock = FakeClock()
    calls: dict[str, list[float]] = {}
    lock = Lock()
    first_request_barrier = Barrier(2)

    def fetch(task, lane):
        with lock:
            lane_calls = calls.setdefault(lane.key, [])
            lane_calls.append(clock.monotonic())
            first_request = len(lane_calls) == 1
        if first_request:
            first_request_barrier.wait(timeout=1)
        return {"task": task["id"], "retrieved_at": clock.now()}

    rows, lane_state, latest = worker._run_parallel_lanes(
        [{"id": value} for value in range(6)],
        concurrency=2,
        min_gap_seconds=4,
        lane_not_before={},
        fetch_task=fetch,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=clock.now,
    )

    assert [row["task"] for row in rows] == list(range(6))
    assert set(calls) == {"lane-0", "lane-1"}
    assert all(times == [0.0, 4.0, 8.0] for times in calls.values())
    assert set(lane_state) == set(calls)
    assert latest == NOW + timedelta(seconds=8)


def test_scale_config_and_egress_pool_keep_credentials_out_of_repr(monkeypatch):
    monkeypatch.setenv("FIRMOTEKA_CATALOG_CONCURRENCY", "3")
    monkeypatch.setenv("FIRMOTEKA_COMPANY_CONCURRENCY", "4")
    monkeypatch.setenv("FIRMOTEKA_BACKPRESSURE_THRESHOLD", "123")
    secret = "do-not-log-this-password"
    monkeypatch.setenv(
        "FIRMOTEKA_EGRESS_POOL_JSON",
        json.dumps(
            [
                {"proxy_url": f"http://lane:{secret}@127.0.0.1:{9000 + index}"}
                for index in range(4)
            ]
        ),
    )

    config = worker.FirmotekaScaleConfig.from_environment()
    lanes = worker._egress_lanes(config.company_concurrency)

    assert config.catalog_concurrency == 3
    assert config.company_concurrency == 4
    assert config.backpressure_threshold == 123
    assert secret not in repr(lanes)
    assert [lane.key for lane in lanes] == [f"lane-{index}" for index in range(4)]

    monkeypatch.setenv(
        "FIRMOTEKA_EGRESS_POOL_JSON",
        f'[{{"proxy_url":"http://lane:{secret}@127.0.0.1:9000"}}',
    )
    with pytest.raises(worker.InvalidDataError) as captured:
        worker._egress_lanes(1)
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


def test_egress_pool_file_is_mode_0600_and_never_repr_leaks_secret(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("FIRMOTEKA_EGRESS_POOL_JSON", raising=False)
    secret = "file-only-egress-password"
    config_path = tmp_path / "egress.json"
    config_path.write_text(
        json.dumps([{"proxy_url": f"http://lane:{secret}@127.0.0.1:9000"}]),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    monkeypatch.setenv("FIRMOTEKA_EGRESS_POOL_FILE", str(config_path))

    lanes = worker._egress_lanes(1)
    assert secret not in repr(lanes)

    config_path.chmod(0o644)
    with pytest.raises(worker.InvalidDataError, match="permissions must be 0600"):
        worker._egress_lanes(1)


def test_partial_parallel_failure_returns_only_failed_checkpoint_for_retry():
    calls = []

    def fetch(task, _lane):
        calls.append(task["id"])
        if task["id"] == 2:
            raise worker.WorkerNetworkError("transient")
        return {"id": task["id"], "retrieved_at": NOW}

    outcomes, _lane_state, _latest = worker._run_parallel_lanes(
        [{"id": 1}, {"id": 2}, {"id": 3}],
        concurrency=2,
        min_gap_seconds=4,
        lane_not_before={},
        fetch_task=fetch,
        sleep=lambda _seconds: None,
    )

    assert calls == [1, 3, 2] or sorted(calls) == [1, 2, 3]
    assert [item["id"] for item in outcomes if isinstance(item, dict)] == [1, 3]
    failure = next(item for item in outcomes if isinstance(item, worker._LaneFailure))
    assert failure.task == {"id": 2}


def test_active_crawl_applies_new_lane_config_between_jobs(tmp_path):
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            ensure_source_factory_datasets(session)
            if (
                session.get(
                    WorkerHandlerRegistration,
                    (worker.SOURCE_ID, worker.HANDLER_VERSION),
                )
                is None
            ):
                session.add(
                    WorkerHandlerRegistration(
                        source_id=worker.SOURCE_ID,
                        handler_version=worker.HANDLER_VERSION,
                        approved=True,
                        enabled=True,
                        live_mode=False,
                        metadata_json={},
                    )
                )
            crawl = _crawl(phase="companies", catalog_concurrency=1, company_concurrency=1)
            session.add(crawl)
            session.flush()
            inn = _legal_inn(150_000_000)
            session.add(
                FirmotekaCrawlItem(
                    crawl_run_id=crawl.id,
                    position=1,
                    inn=inn,
                    source_url=f"https://firmoteka.ru/{inn}",
                    discovered_from="test",
                    status="pending",
                )
            )
            session.flush()

            creation = worker.schedule_firmoteka_check(
                session,
                raw_root=tmp_path,
                now=NOW,
                scale_config=worker.FirmotekaScaleConfig(
                    catalog_concurrency=2,
                    company_concurrency=4,
                    daily_refresh_horizon_days=7,
                    daily_refresh_budget=0,
                ),
            )

            assert creation.job.schedule_metadata["company_concurrency"] == 4
            assert crawl.catalog_concurrency == 2
            assert crawl.company_concurrency == 4
            assert crawl.request_delay_seconds == 4
            assert crawl.daily_refresh_horizon_days == 7
            assert crawl.daily_refresh_budget >= 1
    finally:
        transaction.rollback()
        connection.close()


def test_scale_config_can_advance_at_publisher_job_boundary(tmp_path):
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            crawl = _crawl(
                phase="companies", catalog_concurrency=1, company_concurrency=1
            )
            session.add(crawl)
            session.flush()

            worker._apply_scale_config(
                session,
                crawl,
                worker.FirmotekaScaleConfig(
                    catalog_concurrency=2,
                    company_concurrency=4,
                    min_request_gap_seconds=4,
                    backpressure_threshold=3000,
                    daily_refresh_horizon_days=5,
                    daily_refresh_budget=11,
                ),
            )

            assert crawl.catalog_concurrency == 2
            assert crawl.company_concurrency == 4
            assert crawl.concurrency == 4
            assert crawl.request_delay_seconds == 4
            assert crawl.backpressure_threshold == 3000
            assert crawl.daily_refresh_horizon_days == 5
            assert crawl.daily_refresh_budget == 11
    finally:
        transaction.rollback()
        connection.close()


def test_run_benchmark_metrics_persist_exact_status_latency_and_raw_growth():
    result = worker.HandlerResult(
        raw_artifacts=(
            worker.RawArtifactReference(
                artifact_reference="file:///raw/a",
                checksum="a" * 64,
                manifest={"response_size": 11},
            ),
            worker.RawArtifactReference(
                artifact_reference="file:///raw/b",
                checksum="b" * 64,
                manifest={"response_size": 13},
            ),
        ),
        counters=worker.ExecutionCounters(records_rejected=1),
    )

    metrics = worker._run_benchmark_metrics(
        {
            "lane_count": 2,
            "request_metrics": [
                {"http_status": 200, "latency_ms": 100},
                {"http_status": 200, "latency_ms": 200},
                {"http_status": 429, "latency_ms": 900},
            ],
        },
        result,
    )

    assert metrics == {
        "concurrency": 2,
        "requests": 3,
        "http_status_counts": {"200": 2, "429": 1},
        "latency_ms_p50": 200,
        "latency_ms_p95": 900,
        "latency_ms_max": 900,
        "raw_bytes": 24,
        "parser_rejected": 1,
    }


def test_backpressure_claims_company_batch_once_with_job_identity(tmp_path):
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            crawl = _crawl()
            session.add(crawl)
            session.flush()
            for position in range(2):
                url = f"https://firmoteka.ru/catalog-{position}.xml"
                session.add(
                    FirmotekaCatalogPage(
                        crawl_run_id=crawl.id,
                        position=position,
                        url=url,
                        url_hash=f"{position:064x}",
                        status="pending",
                    )
                )
            for position in range(3):
                inn = _legal_inn(100_000_000 + position)
                session.add(
                    FirmotekaCrawlItem(
                        crawl_run_id=crawl.id,
                        position=position,
                        inn=inn,
                        source_url=f"https://firmoteka.ru/{inn}",
                        discovered_from="test",
                        status="pending",
                    )
                )
            session.flush()

            creation = worker._create_next_job(
                session, crawl=crawl, raw_root=str(tmp_path), now=NOW
            )
            worker._record_request_metrics(
                crawl,
                [
                    {"http_status": 200, "latency_ms": 120},
                    {"http_status": 304, "latency_ms": 620},
                ],
            )
            session.flush()
            session.expire(crawl)
            claimed = tuple(
                session.scalars(
                    select(FirmotekaCrawlItem)
                    .where(FirmotekaCrawlItem.crawl_run_id == crawl.id)
                    .order_by(FirmotekaCrawlItem.position)
                )
            )

            assert creation is not None and creation.created is True
            assert creation.job.job_type == "firmoteka_company_batch"
            assert len(creation.job.schedule_metadata["items"]) == 3
            assert creation.job.schedule_metadata["company_concurrency"] == 2
            assert all(item.status == "running" for item in claimed)
            assert all(item.claimed_by_job_id == creation.job.id for item in claimed)
            assert all(item.claimed_at == NOW for item in claimed)
            assert crawl.http_status_counts == {"200": 1, "304": 1}
            assert crawl.latency_ms_total == 740
            assert crawl.latency_ms_max == 620
            assert crawl.latency_histogram == {"le_250": 1, "le_1000": 1}
    finally:
        transaction.rollback()
        connection.close()


def test_failed_checkpoint_is_reclaimed_with_new_idempotency_generation(tmp_path):
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            crawl = _crawl(phase="companies", backpressure_threshold=1)
            session.add(crawl)
            session.flush()
            failed_job = WorkerJob(
                source_id=worker.SOURCE_ID,
                job_type="firmoteka_company_batch",
                handler_version=worker.HANDLER_VERSION,
                schedule_metadata={"crawl_run_id": str(crawl.id)},
                idempotency_key=f"failed-{crawl.id}",
                status="failed",
                max_attempts=1,
                timeout_seconds=180,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(failed_job)
            session.flush()
            inn = _legal_inn(200_000_000)
            item = FirmotekaCrawlItem(
                crawl_run_id=crawl.id,
                position=1,
                inn=inn,
                source_url=f"https://firmoteka.ru/{inn}",
                discovered_from="test",
                status="running",
                claimed_by_job_id=failed_job.id,
                claimed_at=NOW,
            )
            session.add(item)
            session.flush()

            creation = worker._create_next_job(
                session,
                crawl=crawl,
                raw_root=str(tmp_path),
                now=NOW + timedelta(minutes=1),
            )
            session.flush()

            assert creation is not None and creation.created is True
            assert creation.job.id != failed_job.id
            assert ":resume:" in creation.job.idempotency_key
            assert item.status == "running"
            assert item.attempt_count == 1
            assert item.claimed_by_job_id == creation.job.id
    finally:
        transaction.rollback()
        connection.close()


def test_daily_refresh_horizon_budget_seeds_only_stale_snapshots(
    monkeypatch, tmp_path
):
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            ensure_source_factory_datasets(session)
            dataset = session.scalar(
                select(DataSet).where(DataSet.code == worker.DATASET_CODE)
            )
            if (
                session.get(
                    WorkerHandlerRegistration,
                    (worker.SOURCE_ID, worker.HANDLER_VERSION),
                )
                is None
            ):
                session.add(
                    WorkerHandlerRegistration(
                        source_id=worker.SOURCE_ID,
                        handler_version=worker.HANDLER_VERSION,
                        approved=True,
                        enabled=True,
                        live_mode=False,
                        metadata_json={},
                    )
                )
            prior = _crawl(
                status="completed",
                phase="complete",
                completed_at=NOW - timedelta(days=1),
            )
            session.add(prior)
            raw_sha = "a" * 64
            session.add(
                FirmotekaRawArtifact(
                    sha256=raw_sha,
                    artifact_kind="company",
                    source_url="https://firmoteka.ru/",
                    stored_path="/worker/raw/firmoteka/a/response.html.gz",
                    media_type="text/html",
                    size_bytes=1,
                    retrieved_at=NOW - timedelta(days=90),
                    http_status=200,
                    response_headers={},
                    parser_version="test",
                    manifest={},
                )
            )
            session.flush()
            stale_inn = _legal_inn(300_000_000)
            recent_inn = _legal_inn(300_000_001)
            for inn, age in ((stale_inn, 60), (recent_inn, 2)):
                session.add(
                    FirmotekaCompanySnapshot(
                        dataset_id=dataset.id,
                        company_id=None,
                        inn=inn,
                        source_url=f"https://firmoteka.ru/{inn}",
                        retrieved_at=NOW - timedelta(days=age),
                        raw_sha256=raw_sha,
                        normalized_sha256=inn.ljust(64, "0"),
                        normalized_path=f"/worker/normalized/{inn}.json",
                        content_hash=inn.rjust(64, "0"),
                        projection={},
                        is_current=True,
                    )
                )
            session.flush()
            secret = "must-not-be-persisted"
            monkeypatch.setenv(
                "FIRMOTEKA_EGRESS_POOL_JSON",
                json.dumps([{"proxy_url": f"http://user:{secret}@127.0.0.1:9000"}]),
            )

            creation = worker.schedule_firmoteka_check(
                session,
                raw_root=tmp_path,
                now=NOW,
                scale_config=worker.FirmotekaScaleConfig(
                    daily_refresh_horizon_days=30,
                    daily_refresh_budget=1,
                ),
            )
            session.flush()
            crawl = session.scalar(
                select(FirmotekaCrawlRun).where(FirmotekaCrawlRun.status == "running")
            )
            refresh_items = tuple(
                session.scalars(
                    select(FirmotekaCrawlItem).where(
                        FirmotekaCrawlItem.crawl_run_id == crawl.id
                    )
                )
            )

            assert crawl.run_kind == "daily"
            assert crawl.cursor["daily_refresh_seeded"] == 1
            assert [(item.inn, item.status) for item in refresh_items] == [
                (stale_inn, "pending")
            ]
            assert secret not in json.dumps(creation.job.schedule_metadata)
    finally:
        transaction.rollback()
        connection.close()
