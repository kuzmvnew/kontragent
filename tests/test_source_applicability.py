from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.database.postgres import engine
from app.models.company import Company
from app.models.factory import FactoryGeneration, FactoryGenerationCompany
from app.models.registry_master import CompanyRegistryChange, MasterReplaySignal
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerHandlerRegistration
from app.services.company_enrichment_service import (
    SourceApplicabilityUnknownError,
    _pending_signal_map,
    freeze_operational_sources,
)
from app.services.factory_generation_service import (
    _schedule_source_members,
    process_factory_generations,
)
from app.services.factory_metrics_service import collect_factory_metrics
from app.services.factory_scale_service import collect_factory_pressure
from app.services.source_applicability_service import (
    SourceApplicability,
    resolve_source_applicability,
    source_applicability_expression,
)
from app.services.source_factory_registry_service import (
    DATASETS,
    _factory_metadata,
    registry_applicability_inventory,
)


NOW = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
LEGAL = {"entity_types": ["legal"]}
IP = {"entity_types": ["individual_entrepreneur"]}
BOTH = {"entity_types": ["legal", "individual_entrepreneur"]}


@pytest.mark.parametrize(
    ("metadata", "entity_type", "inn", "expected"),
    (
        (LEGAL, "legal", "7701234567", SourceApplicability.APPLICABLE),
        (LEGAL, "individual_entrepreneur", "123456789012", SourceApplicability.NOT_APPLICABLE),
        (IP, "individual_entrepreneur", "123456789012", SourceApplicability.APPLICABLE),
        (IP, "legal", "7701234567", SourceApplicability.NOT_APPLICABLE),
        (BOTH, "legal", "7701234567", SourceApplicability.APPLICABLE),
        (BOTH, "individual_entrepreneur", "123456789012", SourceApplicability.APPLICABLE),
        (None, "legal", "7701234567", SourceApplicability.UNKNOWN),
        ({}, "legal", "7701234567", SourceApplicability.UNKNOWN),
        ({"entity_types": []}, "legal", "7701234567", SourceApplicability.UNKNOWN),
        ({"entity_types": ["person"]}, "legal", "7701234567", SourceApplicability.UNKNOWN),
        ({"entity_types": "legal"}, "legal", "7701234567", SourceApplicability.UNKNOWN),
        (LEGAL, None, "7701234567", SourceApplicability.UNKNOWN),
        (LEGAL, "company", "7701234567", SourceApplicability.UNKNOWN),
        (LEGAL, "legal", "123456789012", SourceApplicability.UNKNOWN),
        (IP, "individual_entrepreneur", "7701234567", SourceApplicability.UNKNOWN),
    ),
)
def test_python_and_postgresql_applicability_parity(
    metadata, entity_type, inn, expected
):
    company = SimpleNamespace(entity_type=entity_type, inn=inn)
    dataset = SimpleNamespace(applicability=metadata)
    assert resolve_source_applicability(company, dataset) is expected

    with Session(engine) as session:
        actual = session.scalar(
            sa.select(
                source_applicability_expression(
                    sa.literal(metadata, type_=JSONB),
                    sa.literal(entity_type),
                    sa.literal(inn),
                )
            )
        )
        assert actual == expected.value


def test_persisted_policy_wins_over_legacy_source_identity():
    company = SimpleNamespace(
        entity_type="individual_entrepreneur",
        inn="123456789012",
    )
    dataset = SimpleNamespace(code="fns_egrul", applicability=IP)
    assert (
        resolve_source_applicability(company, dataset)
        is SourceApplicability.APPLICABLE
    )


def test_factory_registry_has_only_explicit_valid_applicability():
    inventory = registry_applicability_inventory()
    assert inventory == {
        "total": len(DATASETS),
        "valid": len(DATASETS),
        "missing": 0,
        "invalid": 0,
    }


def test_new_registry_source_without_applicability_fails_validation():
    spec = dict(DATASETS["firmoteka"])
    spec.pop("applicability")
    with pytest.raises(ValueError, match="explicit valid applicability"):
        _factory_metadata("future_source", spec)


def test_factory_migration_does_not_fabricate_applicability(monkeypatch):
    from migrations.versions import b7d3e5f1a9c2_add_company_factory_generations as migration

    executed: list[str] = []

    class OperationRecorder:
        def __getattr__(self, name):
            def record(*args, **_kwargs):
                if name == "execute":
                    executed.append(str(args[0]))

            return record

    monkeypatch.setattr(migration, "op", OperationRecorder())
    migration.upgrade()
    dataset_backfill = next(
        statement for statement in executed if "UPDATE data_sets AS d" in statement
    )
    assert "applicability =" not in dataset_backfill


def _operational_dataset(session: Session, *, code: str, applicability):
    source = DataSource(
        code=f"source-{code}",
        name=code,
        source_type="official",
        enabled=True,
    )
    session.add(source)
    session.flush()
    dataset = DataSet(
        source_id=source.id,
        code=code,
        name=code,
        domain="test",
        update_mode="api",
        data_format="json",
        enabled=True,
        dataset_kind="on_demand_api",
        freshness_policy="daily",
        last_success_at=NOW,
        checked_at=NOW,
        coverage={"operational_accepted": True},
        operational_status="current",
        auto_update_status="configured",
        applicability=applicability,
    )
    session.add(dataset)
    session.flush()
    session.add(
        WorkerHandlerRegistration(
            source_id=code,
            handler_version="applicability-test-v1",
            approved=True,
            enabled=True,
            live_mode=False,
            metadata_json={"mode": "bounded_daily_master_exact_inn_sweep"},
        )
    )
    session.flush()
    return dataset


def test_exact_qa_legal_only_new_source_is_never_actionable(tmp_path, monkeypatch):
    suffix = uuid4().hex[:10]
    code = f"qa_legal_only_{suffix}"
    with Session(engine) as session:
        dataset = _operational_dataset(session, code=code, applicability=LEGAL)
        company = Company(
            inn=f"{uuid4().int:032d}"[-12:],
            name="QA IP applicability company",
            entity_type="individual_entrepreneur",
            status="ACTIVE",
        )
        session.add(company)
        session.flush()
        assert (
            resolve_source_applicability(company, dataset)
            is SourceApplicability.NOT_APPLICABLE
        )
        assert freeze_operational_sources(
            session, company, source_ids=(code,), now=NOW
        ) == ()

        generation = FactoryGeneration(
            generation_type="source",
            generation_key=f"source-operational:{code}",
            source_id=code,
            dataset_id=dataset.id,
            status="running",
            cursor_company_id=company.id - 1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(generation)
        session.flush()
        monkeypatch.setattr(
            "app.services.company_enrichment_service.create_enrichment_run",
            lambda *_args, **_kwargs: pytest.fail("non-applicable work was scheduled"),
        )
        jobs, runs = _schedule_source_members(
            session, generation, limit=10_000, now=NOW
        )
        assert (jobs, runs) == (0, 0)
        assert session.scalar(
            sa.select(FactoryGenerationCompany).where(
                FactoryGenerationCompany.generation_id == generation.id,
                FactoryGenerationCompany.company_id == company.id,
            )
        ) is None

        baseline_pressure = collect_factory_pressure(session, raw_root=tmp_path, now=NOW)
        baseline_metrics = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        change = CompanyRegistryChange(
            source_id=f"qa-{suffix}",
            company_id=company.id,
            run_id=None,
            inn=company.inn,
            event_type="created",
            changed_fields={"created": True},
            source_data_date=NOW.date(),
            source_record_key=f"qa:{suffix}",
        )
        session.add(change)
        session.flush()
        session.add(
            MasterReplaySignal(
                company_id=company.id,
                target_source_id=code,
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
        assert pressure.actionable_backlog == baseline_pressure.actionable_backlog
        assert (
            metrics["queues"]["master_replay_actionable"]
            == baseline_metrics["queues"]["master_replay_actionable"]
        )
        assert (
            metrics["queues"]["master_replay_not_applicable"]
            == baseline_metrics["queues"]["master_replay_not_applicable"] + 1
        )
        assert _pending_signal_map(session, company.id, now=NOW) == {}
        session.rollback()


def test_unknown_dataset_policy_blocks_frozen_plan_and_generation():
    suffix = uuid4().hex[:10]
    code = f"qa_unknown_{suffix}"
    with Session(engine) as session:
        dataset = _operational_dataset(session, code=code, applicability=None)
        company = Company(
            inn=f"{uuid4().int:032d}"[-10:],
            name="QA unknown applicability company",
            entity_type="legal",
            status="ACTIVE",
        )
        session.add(company)
        session.flush()
        with pytest.raises(SourceApplicabilityUnknownError):
            freeze_operational_sources(
                session, company, source_ids=(code,), now=NOW
            )

        generation = FactoryGeneration(
            generation_type="source",
            generation_key=f"source-operational:{code}",
            source_id=code,
            dataset_id=dataset.id,
            status="pending",
            cursor_company_id=company.id - 1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(generation)
        session.flush()
        result = process_factory_generations(session, selection_limit=10_000, now=NOW)
        assert result["runs_scheduled"] == 0
        assert result["status"] == "failed"
        assert result["applicability_unknown_count"] == 0
        assert generation.completed_count == 0
        assert generation.last_error == "Source dataset applicability is unknown"
        assert session.scalar(
            sa.select(sa.func.count(FactoryGenerationCompany.id)).where(
                FactoryGenerationCompany.generation_id == generation.id
            )
        ) == 0
        session.rollback()


def test_unknown_company_scope_is_blocked_and_observable(tmp_path):
    suffix = uuid4().hex[:10]
    code = f"qa_unknown_company_{suffix}"
    with Session(engine) as session:
        dataset = _operational_dataset(session, code=code, applicability=BOTH)
        company = Company(
            inn=f"{uuid4().int:032d}"[-10:],
            name="QA unresolved company subject",
            entity_type=None,
            status="ACTIVE",
        )
        session.add(company)
        session.flush()
        generation = FactoryGeneration(
            generation_type="source",
            generation_key=f"source-operational:{code}",
            source_id=code,
            dataset_id=dataset.id,
            status="pending",
            cursor_company_id=company.id - 1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(generation)
        session.flush()
        result = process_factory_generations(session, selection_limit=10_000, now=NOW)
        assert result["runs_scheduled"] == 0
        assert result["status"] == "failed"
        assert result["applicability_unknown_count"] == 1
        member = session.scalar(
            sa.select(FactoryGenerationCompany).where(
                FactoryGenerationCompany.generation_id == generation.id,
                FactoryGenerationCompany.company_id == company.id,
            )
        )
        assert member.status == "applicability_unknown"
        assert member.completed_at is None
        assert generation.completed_count == 0

        baseline_pressure = collect_factory_pressure(session, raw_root=tmp_path, now=NOW)
        baseline_metrics = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        assert (
            baseline_metrics["queues"][
                "source_generation_applicability_unknown"
            ]
            == 1
        )
        change = CompanyRegistryChange(
            source_id=f"qa-{suffix}",
            company_id=company.id,
            run_id=None,
            inn=company.inn,
            event_type="created",
            changed_fields={"created": True},
            source_data_date=NOW.date(),
            source_record_key=f"qa:{suffix}",
        )
        session.add(change)
        session.flush()
        session.add(
            MasterReplaySignal(
                company_id=company.id,
                target_source_id=code,
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
        assert pressure.actionable_backlog == baseline_pressure.actionable_backlog
        assert (
            metrics["queues"]["master_replay_applicability_unknown"]
            == baseline_metrics["queues"]["master_replay_applicability_unknown"] + 1
        )
        assert (
            metrics["queues"]["source_generation_applicability_unknown"]
            == baseline_metrics["queues"]["source_generation_applicability_unknown"]
        )
        assert _pending_signal_map(session, company.id, now=NOW) == {}
        session.rollback()
