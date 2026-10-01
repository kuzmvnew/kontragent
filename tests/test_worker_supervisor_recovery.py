from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.models.firmoteka import FirmotekaCrawlItem, FirmotekaCrawlRun
from app.models.worker import WorkerJob, WorkerLease, WorkerRun
from app.worker.contracts import HandlerResult
from app.worker.execution import create_job, register_handler
from app.worker.registry import HandlerRegistry
from scripts import run_data_readiness_scheduler as supervisor


NOW = datetime(2026, 10, 1, 6, tzinfo=timezone.utc)


@pytest.fixture
def controlled_db():
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


def _controlled_state(*, queued=3):
    return {
        "jobs_by_status": {
            "queued": queued,
            "running": 0,
            "retry_scheduled": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        },
        "worker_job_count": queued,
        "firmoteka_job_count": queued,
        "raw_count": 5,
        "snapshot_count": 7,
        "firmoteka_crawls": {},
    }


def test_supervisor_recovers_stale_runs_before_claiming(monkeypatch):
    events = []

    class Session:
        def __enter__(self):
            events.append("session_enter")
            return self

        def __exit__(self, *_args):
            events.append("session_exit")

        def commit(self):
            events.append("commit")

    class Executor:
        def __init__(self, **_kwargs):
            events.append("executor")

        def run_once(self, *, allowed_lanes=None):
            assert allowed_lanes == ("master_intake",)
            events.append("claim")
            return None

    def recover(session, *, stale_after, retry_policy):
        assert isinstance(session, Session)
        assert stale_after == timedelta(minutes=2)
        assert isinstance(retry_policy, supervisor.RetryPolicy)
        events.append("recover")
        return ()

    def enrich(session, **_kwargs):
        assert isinstance(session, Session)
        events.append("enrich")
        return {}

    monkeypatch.setattr(supervisor, "SessionLocal", Session)
    monkeypatch.setattr(supervisor, "recover_stale_runs", recover)
    monkeypatch.setattr(supervisor, "run_company_enrichment_cycle", enrich)
    monkeypatch.setattr(supervisor, "WorkerExecutor", Executor)

    assert supervisor.run_workers(object(), max_jobs=1) == []
    assert events.index("recover") < events.index("commit") < events.index("claim")


def test_prepare_worker_state_does_not_claim_source_job(monkeypatch):
    events = []

    class Session:
        def __enter__(self):
            events.append("session_enter")
            return self

        def __exit__(self, *_args):
            events.append("session_exit")

        def commit(self):
            events.append("commit")

    def recover(_session, **_kwargs):
        events.append("recover")
        return ("run-1",)

    def enrich(_session, **kwargs):
        events.append(("enrich", kwargs))
        return {"runs_created": 1}

    class Executor:
        def __init__(self, **_kwargs):
            raise AssertionError("preparation must not construct an executor")

    monkeypatch.setattr(supervisor, "SessionLocal", Session)
    monkeypatch.setattr(supervisor, "recover_stale_runs", recover)
    monkeypatch.setattr(supervisor, "run_company_enrichment_cycle", enrich)
    monkeypatch.setattr(supervisor, "WorkerExecutor", Executor)

    result = supervisor.prepare_worker_state(max_jobs=2)

    assert result == {
        "recovered_run_ids": ("run-1",),
        "enrichment": {"runs_created": 1},
    }
    assert events == [
        "session_enter",
        "recover",
        ("enrich", {"signal_limit": 50, "reconcile_limit": 100}),
        "commit",
        "session_exit",
    ]


def test_controlled_interval_is_one_shot_budgeted_and_scheduler_free(monkeypatch):
    events = []
    recovered_id = uuid4()
    claims = [
        SimpleNamespace(
            job_id=uuid4(),
            run_id=uuid4(),
            source_id="firmoteka",
            work_units=10,
            schedule_metadata={"items": [{} for _ in range(10)]},
        ),
        SimpleNamespace(
            job_id=uuid4(),
            run_id=uuid4(),
            source_id="firmoteka",
            work_units=10,
            schedule_metadata={"items": [{} for _ in range(10)]},
        ),
    ]

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

    class Executor:
        lease_ttl = timedelta(seconds=60)

        def __init__(self, **_kwargs):
            pass

        def execute_claim(self, claim):
            events.append(("execute", claim.work_units))
            return claim.run_id

    after = _controlled_state(queued=1)
    after["worker_job_count"] = 3
    after["firmoteka_job_count"] = 3
    snapshots = iter((_controlled_state(), after))

    def claim(_session, _registry, **kwargs):
        events.append(
            (
                "claim",
                kwargs["allowed_source_ids"],
                kwargs["allowed_lanes"],
                kwargs["max_work_units"],
            )
        )
        return claims.pop(0) if claims else None

    monkeypatch.setattr(supervisor, "SessionLocal", Session)
    monkeypatch.setattr(supervisor, "WorkerExecutor", Executor)
    monkeypatch.setattr(supervisor, "capture_controlled_state", lambda _session: next(snapshots))
    monkeypatch.setattr(supervisor, "claim_next_job", claim)
    monkeypatch.setattr(
        supervisor,
        "recover_stale_runs",
        lambda *_args, **_kwargs: (recovered_id,),
    )
    monkeypatch.setattr(
        supervisor,
        "run_due_updates",
        lambda: (_ for _ in ()).throw(AssertionError("scheduler must remain off")),
    )
    monkeypatch.setattr(
        supervisor,
        "run_company_enrichment_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("enrichment must remain off")
        ),
    )

    report = supervisor.run_controlled_interval(
        object(),
        allowed_source_ids=("firmoteka",),
        allowed_lanes=("master_intake",),
        work_budget=20,
    )

    assert report["scheduler_jobs_created"] == 0
    assert report["runs_created"] == 2
    assert report["sources_claimed"] == {"firmoteka": 2}
    assert report["work_units_admitted"] == 20
    assert report["work_units_remaining"] == 0
    assert report["firmoteka_jobs_claimed"] == 2
    assert report["firmoteka_work_units_admitted"] == 20
    assert report["firmoteka_child_items_admitted"] == 20
    assert report["enrichment_mutations"] == 0
    assert report["stale_runs_recovered"] == {
        "count": 1,
        "run_ids": [str(recovered_id)],
    }
    assert ("claim", ("firmoteka",), ("master_intake",), 20) in events
    assert ("claim", ("firmoteka",), ("master_intake",), 10) in events


def test_controlled_interval_without_source_admission_claims_nothing(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

    class Executor:
        lease_ttl = timedelta(seconds=60)

        def __init__(self, **_kwargs):
            pass

    snapshots = iter((_controlled_state(), _controlled_state()))
    observed = []

    def claim(_session, _registry, **kwargs):
        observed.append(kwargs["allowed_source_ids"])
        return None

    monkeypatch.setattr(supervisor, "SessionLocal", Session)
    monkeypatch.setattr(supervisor, "WorkerExecutor", Executor)
    monkeypatch.setattr(supervisor, "capture_controlled_state", lambda _session: next(snapshots))
    monkeypatch.setattr(supervisor, "claim_next_job", claim)
    monkeypatch.setattr(supervisor, "recover_stale_runs", lambda *_args, **_kwargs: ())

    report = supervisor.run_controlled_interval(
        object(), allowed_source_ids=(), work_budget=20
    )

    assert observed == []
    assert report["runs_created"] == 0
    assert report["sources_claimed"] == {}
    assert report["firmoteka_jobs_claimed"] == 0
    assert report["work_units_admitted"] == 0
    assert report["work_units_remaining"] == 20


def test_real_controlled_interval_rejects_undersized_firmoteka_override(
    monkeypatch, controlled_db
):
    registry = HandlerRegistry()
    handler_version = f"controlled-cost-{uuid4().hex}"

    def empty_handler(_context):
        return HandlerResult()

    with controlled_db() as session:
        register_handler(
            session,
            registry,
            source_id="firmoteka",
            version=handler_version,
            handler=empty_handler,
            approved=True,
            fixture=True,
            metadata={"task": "STARTUP-BURST-CONTROL-01-CORRECTION-01"},
        )
        crawl = FirmotekaCrawlRun(
            run_kind="initial",
            status="running",
            phase="companies",
            cursor={},
            request_delay_seconds=4,
            concurrency=1,
            catalog_concurrency=1,
            company_concurrency=1,
            backpressure_threshold=2000,
            daily_refresh_horizon_days=7,
            daily_refresh_budget=1,
            lane_request_state={},
            started_at=NOW,
        )
        session.add(crawl)
        session.flush()
        items = [
            FirmotekaCrawlItem(
                crawl_run_id=crawl.id,
                position=position,
                inn=f"{position:010d}",
                source_url=f"https://firmoteka.test/company/{position}",
                discovered_from="controlled-cost-regression",
                status="pending",
            )
            for position in range(1, 41)
        ]
        session.add_all(items)
        session.flush()
        creation = create_job(
            session,
            source_id="firmoteka",
            job_type="firmoteka_company_batch",
            handler_version=handler_version,
            idempotency_key=f"controlled-cost-{uuid4().hex}",
            schedule_metadata={
                "crawl_run_id": str(crawl.id),
                "items": [
                    {"id": item.id, "inn": item.inn, "url": item.source_url}
                    for item in items
                ],
                "controlled_work_units": 1,
                "factory_lane": "master_intake",
            },
            now=NOW,
        )
        for item in items:
            item.status = "running"
            item.claimed_by_job_id = creation.job.id
            item.claimed_at = NOW
        session.commit()
        job_id = creation.job.id
        item_ids = tuple(item.id for item in items)

    with controlled_db() as session:
        item_state_before = tuple(
            session.execute(
                sa.select(
                    FirmotekaCrawlItem.id,
                    FirmotekaCrawlItem.status,
                    FirmotekaCrawlItem.attempt_count,
                    FirmotekaCrawlItem.claimed_by_job_id,
                    FirmotekaCrawlItem.claim_fencing_token,
                    FirmotekaCrawlItem.claimed_at,
                )
                .where(FirmotekaCrawlItem.id.in_(item_ids))
                .order_by(FirmotekaCrawlItem.id)
            ).all()
        )
        runs_before = session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkerRun)
            .where(WorkerRun.job_id == job_id)
        )

    monkeypatch.setattr(supervisor, "SessionLocal", controlled_db)
    report = supervisor.run_controlled_interval(
        registry,
        allowed_source_ids=("firmoteka",),
        allowed_lanes=("master_intake",),
        work_budget=20,
    )

    assert report["runs_created"] == 0
    assert report["work_units_admitted"] == 0
    assert report["work_units_remaining"] == 20
    assert report["firmoteka_jobs_claimed"] == 0
    assert report["firmoteka_child_items_admitted"] == 0
    with controlled_db() as session:
        job = session.get(WorkerJob, job_id)
        assert job.status in {"queued", "retry_scheduled"}
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkerRun)
            .where(WorkerRun.job_id == job_id)
        ) == runs_before
        assert session.get(WorkerLease, "firmoteka") is None
        assert tuple(
            session.execute(
                sa.select(
                    FirmotekaCrawlItem.id,
                    FirmotekaCrawlItem.status,
                    FirmotekaCrawlItem.attempt_count,
                    FirmotekaCrawlItem.claimed_by_job_id,
                    FirmotekaCrawlItem.claim_fencing_token,
                    FirmotekaCrawlItem.claimed_at,
                )
                .where(FirmotekaCrawlItem.id.in_(item_ids))
                .order_by(FirmotekaCrawlItem.id)
            ).all()
        ) == item_state_before


def test_controlled_cli_never_dispatches_due_schedules(monkeypatch, capsys):
    registry = object()
    observed = {}
    report = {
        "mode": "controlled_start",
        "scheduler_jobs_created": 0,
        "work_units_admitted": 0,
    }

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_data_readiness_scheduler",
            "--controlled-start",
            "--allow-source",
            "safe-source",
            "--allow-lane",
            "source_control",
        ],
    )
    monkeypatch.setattr(supervisor, "build_registry", lambda: registry)

    def controlled(candidate, **kwargs):
        observed.update({"registry": candidate, **kwargs})
        return report

    monkeypatch.setattr(supervisor, "run_controlled_interval", controlled)
    monkeypatch.setattr(
        supervisor,
        "run_due_updates",
        lambda: (_ for _ in ()).throw(AssertionError("scheduler must remain off")),
    )
    monkeypatch.setattr(
        supervisor,
        "run_workers",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal worker path must remain off")
        ),
    )

    supervisor.main()

    assert observed == {
        "registry": registry,
        "allowed_source_ids": ["safe-source"],
        "allowed_lanes": ["source_control"],
        "work_budget": 20,
    }
    assert json.loads(capsys.readouterr().out) == report
