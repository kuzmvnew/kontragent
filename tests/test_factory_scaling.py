from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.database.postgres import engine
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun
from app.models.factory import FactoryGeneration, FactoryGenerationCompany
from app.models.registry_master import CompanyRegistryChange, MasterReplaySignal
from app.models.source import DataSet, DataSource
from app.services.company_enrichment_service import _pending_signal_map
from app.services.factory_generation_service import (
    ensure_operational_source_generations,
    process_factory_generations,
    register_ruleset_generation,
)
from app.services.factory_metrics_service import collect_factory_metrics
from app.services.factory_scale_service import (
    FactoryPressureInputs,
    FactoryScaleConfig,
    adaptive_intake_decision,
    collect_factory_pressure,
)


NOW = datetime(2026, 9, 27, 8, tzinfo=timezone.utc)


def test_adaptive_backpressure_uses_throughput_and_resource_gates():
    config = FactoryScaleConfig()
    caught_up = FactoryPressureInputs(
        actionable_backlog=0,
        oldest_backlog_seconds=None,
        enrichment_companies_per_hour=0,
        firmoteka_companies_per_hour=0,
        database_connection_percent=5,
        host_cpu_percent=10,
        disk_free_percent=90,
        raw_growth_bytes_per_hour=0,
    )
    assert adaptive_intake_decision(caught_up, config).mode == "open"

    overloaded = FactoryPressureInputs(
        actionable_backlog=500,
        oldest_backlog_seconds=100,
        enrichment_companies_per_hour=100,
        firmoteka_companies_per_hour=400,
        database_connection_percent=5,
        host_cpu_percent=10,
        disk_free_percent=90,
        raw_growth_bytes_per_hour=1,
    )
    decision = adaptive_intake_decision(overloaded, config)
    assert decision.mode == "throttled"
    assert decision.allow_company_intake is True
    assert decision.recommended_intake_ratio == 0.25

    disk_pressure = FactoryPressureInputs(
        **{**caught_up.__dict__, "disk_free_percent": 5}
    )
    decision = adaptive_intake_decision(disk_pressure, config)
    assert decision.mode == "paused"
    assert decision.allow_company_intake is False

    growth_pressure = FactoryPressureInputs(
        **{
            **caught_up.__dict__,
            "raw_growth_bytes_per_hour": 10_000,
            "disk_free_bytes": 100_000,
        }
    )
    decision = adaptive_intake_decision(growth_pressure, config)
    assert decision.mode == "paused"
    assert "raw_growth_exhausts_disk_runway" in decision.reasons


def test_ip_only_signal_does_not_backpressure_legal_company(tmp_path):
    """A stale cross-scope signal must not stall Master intake or public refresh."""

    with Session(engine) as session:
        dataset = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_npd"))
        if dataset is None:
            source = DataSource(
                code=f"factory-npd-{uuid4().hex[:12]}",
                name="Factory NPD regression source",
                source_type="official",
                enabled=True,
            )
            session.add(source)
            session.flush()
            dataset = DataSet(
                source_id=source.id,
                code="fns_npd",
                name="NPD",
                domain="tax",
                update_mode="bulk",
                data_format="json",
                enabled=True,
                dataset_kind="bulk_snapshot",
                freshness_policy="daily",
                last_success_at=NOW,
                source_as_of=NOW,
                checked_at=NOW,
                coverage={"operational_accepted": True},
                operational_status="current",
                auto_update_status="configured",
            )
            session.add(dataset)
        else:
            dataset.enabled = True
            dataset.last_success_at = NOW
            dataset.next_expected_update_at = None
            dataset.coverage = {"operational_accepted": True}
            dataset.operational_status = "current"
            dataset.auto_update_status = "configured"

        baseline = collect_factory_pressure(session, raw_root=tmp_path, now=NOW)
        baseline_metrics = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        company = Company(
            inn=str(uuid4().int)[:10],
            name="Legal company with inapplicable NPD signal",
            entity_type="legal",
            status="ACTIVE",
        )
        session.add(company)
        session.flush()
        change = CompanyRegistryChange(
            source_id="factory-regression",
            company_id=company.id,
            run_id=None,
            inn=company.inn,
            event_type="created",
            changed_fields={"created": True},
            source_data_date=NOW.date(),
            source_record_key=f"record:{uuid4().hex}",
        )
        session.add(change)
        session.flush()
        session.add(
            MasterReplaySignal(
                company_id=company.id,
                target_source_id="fns_npd",
                registry_change_id=change.id,
                status="pending",
                created_at=NOW,
            )
        )
        session.flush()

        pressure = collect_factory_pressure(session, raw_root=tmp_path, now=NOW)
        metrics = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        assert pressure.actionable_backlog == baseline.actionable_backlog
        assert (
            metrics["queues"]["master_replay_actionable"]
            == baseline_metrics["queues"]["master_replay_actionable"]
        )
        assert (
            metrics["queues"]["master_replay_total"]
            == baseline_metrics["queues"]["master_replay_total"] + 1
        )
        assert _pending_signal_map(session, company.id, now=NOW) == {}
        session.rollback()


def test_ruleset_generation_is_idempotent():
    with Session(engine) as session:
        first, created = register_ruleset_generation(
            session,
            generation_type="semantic_ruleset",
            ruleset_version=f"semantic-test-{uuid4().hex}",
            now=NOW,
        )
        assert created is True
        repeated, created = register_ruleset_generation(
            session,
            generation_type="semantic_ruleset",
            ruleset_version=first.ruleset_version,
            now=NOW,
        )
        assert created is False
        assert repeated.id == first.id
        session.rollback()


def test_new_operational_source_schedules_source_only_master_backfill(monkeypatch):
    suffix = uuid4().hex[:12]
    source_code = f"factory_source_{suffix}"
    with Session(engine) as session:
        source = DataSource(
            code=f"factory_{suffix}",
            name="Factory generation source",
            source_type="official",
            enabled=True,
        )
        company = Company(
            inn=str(uuid4().int)[:10],
            name="Factory generation company",
            entity_type="legal",
            status="ACTIVE",
        )
        session.add_all((source, company))
        session.flush()
        dataset = DataSet(
            source_id=source.id,
            code=source_code,
            name="Factory source",
            domain="test",
            update_mode="bulk",
            data_format="json",
            enabled=True,
            dataset_kind="bulk_snapshot",
            freshness_policy="daily",
            last_success_at=NOW,
            source_as_of=NOW,
            checked_at=NOW,
            coverage={"operational_accepted": True},
            operational_status="current",
            auto_update_status="configured",
        )
        session.add(dataset)
        session.flush()
        assert ensure_operational_source_generations(session, now=NOW) >= 1
        generation = session.scalar(
            sa.select(FactoryGeneration).where(
                FactoryGeneration.source_id == source_code
            )
        )
        captured = []

        def fake_create(_session, **kwargs):
            captured.append(tuple(kwargs["source_ids"]))
            run = CompanyEnrichmentRun(
                company_id=kwargs["company_id"],
                trigger=kwargs["trigger"],
                idempotency_key=kwargs["idempotency_key"],
                status="pending",
                stage="planning",
                applicable_sources=[],
                source_count=1,
                completed_source_count=0,
                failed_source_count=0,
                public_ready=False,
                started_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            _session.add(run)
            _session.flush()
            return SimpleNamespace(run=run, created=True)

        monkeypatch.setattr(
            "app.services.company_enrichment_service.create_enrichment_run",
            fake_create,
        )
        monkeypatch.setattr(
            "app.services.company_enrichment_service.enqueue_enrichment_work",
            lambda *_args, **_kwargs: 1,
        )
        paused = process_factory_generations(
            session,
            selection_limit=10_000,
            source_selection_limit=0,
            now=NOW,
        )
        assert paused["runs_scheduled"] == 0
        assert captured == []
        result = process_factory_generations(session, selection_limit=10_000, now=NOW)
        assert result["generation_id"] == str(generation.id)
        assert result["runs_scheduled"] >= 1
        assert captured and set(captured) == {(source_code,)}
        member = session.scalar(
            sa.select(FactoryGenerationCompany).where(
                FactoryGenerationCompany.generation_id == generation.id,
                FactoryGenerationCompany.company_id == company.id,
            )
        )
        assert member.status == "scheduled"
        session.rollback()
