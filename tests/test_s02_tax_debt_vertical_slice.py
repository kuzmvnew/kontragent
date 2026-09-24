from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.contracts.risk_v3 import (
    Execution,
    Freshness,
    Observation,
    OverallRiskResult,
    ResolutionState,
)
from app.contracts.s02_tax_debt import S02FactState
from app.database.postgres import engine
from app.models.company import Company
from app.models.source import DataSet, DataSource
from app.models.tax_debt import (
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
    FnsTaxDebtQuarantineRecord,
    FnsTaxDebtRawArtifact,
)
from app.models.worker import WorkerJob, WorkerPublicationState, WorkerRun
from app.services.s02_tax_debt_vertical_slice_service import (
    build_s02_tax_debt_fact,
    calculate_s02_vertical_slice,
    calculate_s02_vertical_slice_from_persisted,
    s02_tax_debt_fact_to_risk_candidate,
)
from app.sources.fns_tax_debt import PILOT_ENVIRONMENT, SOURCE_ID
from tests.test_risk_v3 import COMPANY_ID, complete_legal_baseline, replace


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
SOURCE_AS_OF = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
DATA_AS_OF = date(2026, 9, 1)
TARGET_INN = "7707083893"
OTHER_INN = "7811089030"


def _fact(state: S02FactState, *, amount=Decimal("125.00")):
    values = {
        "company_id": COMPANY_ID,
        "state": state,
        "checked_at": NOW,
        "evidence_refs": (f"s02-evidence:{state.value}",),
        "source_as_of": SOURCE_AS_OF,
        "retrieved_at": RETRIEVED_AT,
        "official_actual_until": date(2026, 9, 30),
    }
    if state == S02FactState.FOUND:
        values.update(
            amount=amount,
            amount_as_of_date=DATA_AS_OF,
            total_arrears=amount - Decimal("25.00") if amount else Decimal("0"),
            total_penalties=Decimal("20.00") if amount else Decimal("0"),
            total_fines=Decimal("5.00") if amount else Decimal("0"),
            source_reference="file:///internal/raw.zip#record=secret",
            source_document_id="DEBT-1",
            provenance={
                "source_id": "S02",
                "artifact_sha256": "a" * 64,
                "artifact_reference": "file:///internal/raw.zip",
                "worker_run_id": "internal-worker-run",
                "source_document_id": "DEBT-1",
                "source_member": "data.xml",
                "record_hash": "b" * 64,
                "source_as_of": SOURCE_AS_OF.isoformat(),
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "parser_version": "fns-debtam-xml-v1",
                "normalization_version": "tax-debt-normalization-v1",
                "matching_method": "inn_exact",
            },
        )
    elif state == S02FactState.NOT_FOUND:
        values.update(
            amount_as_of_date=DATA_AS_OF,
            limitations=("absence_is_limited_to_current_snapshot",),
        )
    elif state == S02FactState.STALE_DATA:
        values.update(
            amount_as_of_date=DATA_AS_OF,
            freshness_reason="official_actual_until_expired",
            limitations=("stale_data",),
        )
    else:
        values.update(
            freshness_reason="dataset_not_available",
            limitations=("dataset_not_available",),
        )
    return build_s02_tax_debt_fact(**values)


def _candidate(state: S02FactState, *, amount=Decimal("125.00")):
    return s02_tax_debt_fact_to_risk_candidate(
        _fact(state, amount=amount),
        inn="7700000000",
    )


def _slice(state: S02FactState, *, amount=Decimal("125.00")):
    values = replace(
        complete_legal_baseline(),
        "tax_debt",
        _candidate(state, amount=amount),
    )
    return calculate_s02_vertical_slice(
        values,
        company_id=COMPANY_ID,
        calculated_at=NOW,
    )


def test_s02_fact_creation_carries_amount_date_source_freshness_and_provenance():
    fact = _fact(S02FactState.FOUND)

    assert fact.fact_code == "tax.debt.amount_as_of_date"
    assert fact.amount == Decimal("125.00")
    assert fact.amount_as_of_date == DATA_AS_OF
    assert fact.source.source_id == "S02"
    assert fact.freshness.status == Freshness.CURRENT
    assert fact.provenance.artifact_sha256 == "a" * 64
    assert fact.provenance.matching_method == "inn_exact"
    assert "not_real_time_balance" in fact.limitations


def test_s02_fact_is_the_existing_risk_input_without_engine_changes():
    candidate = _candidate(S02FactState.FOUND)

    assert candidate.capability_code == "tax_debt"
    assert candidate.fact_identity == "tax.current_debt"
    assert candidate.observation == Observation.FOUND
    assert candidate.execution == Execution.CHECKED
    assert candidate.fact_payload["adverse"] is True
    assert candidate.fact_payload["total_debt"] == "125.00"
    assert candidate.scope_details["s02_fact"]["state"] == "FOUND"


def test_s02_risk_to_summary_keeps_traceable_confirmed_fact():
    fact, risk, summary, _ = _slice(S02FactState.FOUND)

    tax_check = next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )
    assert tax_check.resolution_state == ResolutionState.RESOLVED
    assert any(item.factor_code == "TAX_DEBT_PRESENT" for item in risk.factors)
    assert any(
        item.fact_code == fact.fact_code
        and item.origin_check_ref == tax_check.check_ref
        for item in summary.confirmed_positive_facts
    )
    assert any(
        ref.endswith("TAX_DEBT_PRESENT") for ref in summary.key_reason_refs
    )


def test_s02_public_projection_contains_card_data_and_hides_raw_coordinates():
    fact, risk, summary, projection = _slice(S02FactState.FOUND)
    payload = projection.model_dump(mode="json")

    assert projection.summary.summary_id == summary.summary_id
    assert projection.risk.assessment_id == risk.assessment_id
    assert projection.evidence.fact_ref == fact.fact_ref
    assert projection.evidence.amount == Decimal("125.00")
    assert projection.evidence.total_arrears == Decimal("100.00")
    assert projection.coverage.resolution_state == ResolutionState.RESOLVED
    assert projection.freshness.status == Freshness.CURRENT
    assert "not_real_time_balance" in projection.limitations
    rendered = str(payload)
    assert "file:///internal" not in rendered
    assert "internal-worker-run" not in rendered
    assert "data.xml" not in rendered
    assert "a" * 64 not in rendered


@pytest.mark.parametrize(
    ("state", "observation", "execution", "freshness", "resolved"),
    (
        (
            S02FactState.FOUND,
            Observation.FOUND,
            Execution.CHECKED,
            Freshness.CURRENT,
            ResolutionState.RESOLVED,
        ),
        (
            S02FactState.NOT_FOUND,
            Observation.NOT_FOUND,
            Execution.CHECKED,
            Freshness.CURRENT,
            ResolutionState.RESOLVED,
        ),
        (
            S02FactState.STALE_DATA,
            Observation.UNKNOWN,
            Execution.CHECKED,
            Freshness.STALE,
            ResolutionState.UNRESOLVED,
        ),
        (
            S02FactState.SOURCE_UNAVAILABLE,
            Observation.UNKNOWN,
            Execution.SOURCE_UNAVAILABLE,
            Freshness.UNKNOWN,
            ResolutionState.UNRESOLVED,
        ),
    ),
)
def test_s02_states_are_preserved_through_risk_and_projection(
    state,
    observation,
    execution,
    freshness,
    resolved,
):
    _, risk, _, projection = _slice(state)
    check = next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )

    assert projection.evidence.state == state
    assert check.observation == observation
    assert check.execution == execution
    assert check.freshness == freshness
    assert check.resolution_state == resolved
    assert projection.coverage.resolution_state == resolved


@pytest.mark.parametrize(
    "state",
    (
        S02FactState.NOT_FOUND,
        S02FactState.STALE_DATA,
        S02FactState.SOURCE_UNAVAILABLE,
    ),
)
def test_no_data_never_creates_false_tax_debt_risk(state):
    _, risk, summary, projection = _slice(state)

    assert all(item.factor_code != "TAX_DEBT_PRESENT" for item in risk.factors)
    assert all(
        item.fact_code != "tax.debt.amount_as_of_date"
        for item in summary.confirmed_positive_facts
    )
    assert projection.risk.factors == ()
    if state == S02FactState.NOT_FOUND:
        assert (
            risk.overall_result
            == OverallRiskResult.NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE
        )
    else:
        assert risk.overall_result == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION


def test_zero_amount_source_record_is_negative_closure_not_risk():
    fact, risk, _, projection = _slice(S02FactState.FOUND, amount=Decimal("0"))
    check = next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )

    assert fact.state == S02FactState.FOUND
    assert fact.has_debt is False
    assert check.observation == Observation.NOT_FOUND
    assert check.negative_closure_proven is True
    assert projection.risk.factors == ()


@pytest.fixture
def s02_db():
    connection = engine.connect()
    transaction = connection.begin()
    assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _coverage(*, matched=0, unmatched=0, conflicts=0, quarantined=0):
    normalized = matched + unmatched + conflicts
    records = normalized + quarantined
    return {
        "xml_members": 1,
        "schema_versions": ["4.01"],
        "source_records": records,
        "cohort_records": records,
        "normalized_records": normalized,
        "quarantined_records": quarantined,
        "identical_duplicates": 0,
        "identity_conflicts": 0,
        "matched_records": matched,
        "unmatched_records": unmatched,
        "entity_conflicts": conflicts,
        "database_duplicates": 0,
        "projected_facts": matched,
        "official_actual_until": "2026-09-30",
    }


def _counters(coverage):
    return {
        "records_seen": coverage["source_records"],
        "records_written": coverage["normalized_records"],
        "records_rejected": coverage["quarantined_records"],
        "records_duplicated": 0,
        "records_published": coverage["projected_facts"],
    }


def _add_worker_run(session, counters, *, suffix: str):
    job = WorkerJob(
        source_id=SOURCE_ID,
        job_type="s02-negative-closure-test",
        handler_version="s02-negative-closure-test-v1",
        schedule_metadata={},
        idempotency_key=f"s02-negative-closure:{suffix}:{uuid4().hex}",
        status="succeeded",
        max_attempts=1,
        timeout_seconds=30,
        next_attempt_at=None,
    )
    session.add(job)
    session.flush()
    run = WorkerRun(
        job_id=job.id,
        attempt_no=1,
        started_at=RETRIEVED_AT,
        finished_at=RETRIEVED_AT,
        status="succeeded",
        worker_id="dev012-correction-test",
        fencing_token=1,
        handler_version=job.handler_version,
        current_stage="completed",
        errors=[],
        checksum_metadata={},
        heartbeat_at=RETRIEVED_AT,
        duration_ms=1,
        **counters,
    )
    session.add(run)
    session.flush()
    return run


def _ensure_s02_dataset(session, coverage):
    source = session.scalar(sa.select(DataSource).where(DataSource.code == "fns"))
    if source is None:
        source = DataSource(
            code="fns",
            name="ФНС России",
            source_type="official",
            priority=10,
            enabled=True,
        )
        session.add(source)
        session.flush()
    dataset = session.scalar(
        sa.select(DataSet).where(DataSet.code == "fns_tax_debt")
    )
    if dataset is None:
        dataset = DataSet(
            source_id=source.id,
            code="fns_tax_debt",
            name="ФНС: Налоговая задолженность",
            domain="tax_debt",
            update_mode="bulk",
            data_format="xml",
            refresh_schedule="periodic",
            priority=10,
            enabled=True,
        )
        session.add(dataset)
        session.flush()
    dataset.last_success_at = RETRIEVED_AT
    dataset.last_data_date = DATA_AS_OF
    dataset.source_as_of = SOURCE_AS_OF
    dataset.retrieved_at = RETRIEVED_AT
    dataset.published_at = RETRIEVED_AT
    dataset.record_count = coverage["normalized_records"]
    dataset.coverage = coverage
    dataset.operational_status = "ready"
    dataset.last_error = None
    dataset.last_error_at = None
    return dataset


def _add_artifact(session, dataset, run, *, suffix: int):
    checksum = f"{suffix:064x}"
    raw_pointer = f"file:///private/internal/s02-{suffix}.zip"
    artifact = FnsTaxDebtRawArtifact(
        dataset_id=dataset.id,
        first_worker_run_id=run.id,
        sha256=checksum,
        artifact_reference=raw_pointer,
        original_file_name=f"s02-{suffix}.zip",
        media_type="application/zip",
        size_bytes=1,
        manifest={"source_id": SOURCE_ID},
        source_as_of=SOURCE_AS_OF,
        retrieved_at=RETRIEVED_AT,
    )
    session.add(artifact)
    session.flush()
    return artifact


def _add_normalized(
    session,
    dataset,
    artifact,
    *,
    inn: str,
    match_state: str,
    company_id: int | None = None,
    amount: Decimal = Decimal("0"),
    suffix: int,
):
    record = FnsTaxDebtNormalizedRecord(
        dataset_id=dataset.id,
        artifact_id=artifact.id,
        company_id=company_id,
        source_member="data.xml",
        source_ordinal=suffix,
        inn=inn,
        company_name="ООО TEST",
        source_document_id=f"DOC-{suffix}",
        document_date=DATA_AS_OF,
        data_date=DATA_AS_OF,
        total_arrears=amount,
        total_penalties=Decimal("0"),
        total_fines=Decimal("0"),
        total_debt=amount,
        items=[],
        record_hash=f"{suffix + 100:064x}",
        normalization_version="tax-debt-normalization-v1",
        validation_state="valid",
        match_state=match_state,
        match_method="inn_exact" if match_state == "matched" else None,
        limitation_states=["dated_snapshot"],
    )
    session.add(record)
    session.flush()
    return record


def _add_quarantine(session, dataset, artifact, *, inn: str | None, suffix: int):
    payload = (
        {"document": {}, "taxpayer": {"ИННЮЛ": inn}, "debt_items": []}
        if inn is not None
        else {"document": {}, "taxpayer": {}, "debt_items": []}
    )
    row = FnsTaxDebtQuarantineRecord(
        dataset_id=dataset.id,
        artifact_id=artifact.id,
        source_member="data.xml",
        source_ordinal=suffix,
        raw_hash=f"{suffix + 200:064x}",
        reason_codes=["missing_total_debt"],
        raw_payload=payload,
    )
    session.add(row)
    session.flush()
    return row


def _add_snapshot(session, dataset, record, company, *, amount: Decimal, generation=0):
    snapshot = CompanyTaxDebtSnapshot(
        company_id=company.id,
        dataset_id=dataset.id,
        normalized_record_id=record.id,
        publication_generation=generation,
        data_date=DATA_AS_OF,
        document_date=DATA_AS_OF,
        source_document_id=record.source_document_id,
        total_arrears=amount,
        total_penalties=Decimal("0"),
        total_fines=Decimal("0"),
        total_debt=amount,
        item_count=0,
        source_reference=f"file:///private/internal/raw.zip#record={record.record_hash}",
        provenance={
            "artifact_sha256": record.record_hash,
            "artifact_reference": "file:///private/internal/raw.zip",
            "worker_run_id": "internal-worker-run",
            "source_document_id": record.source_document_id,
            "source_member": "data.xml",
            "record_hash": record.record_hash,
            "matching_method": "inn_exact",
        },
        limitation_states=["dated_snapshot"],
        retrieved_at=RETRIEVED_AT,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _seed_baseline(
    session,
    *,
    mode: str = "negative",
    quarantine_inn: str | None = None,
):
    company = Company(
        inn=TARGET_INN,
        name="DEV-012 target",
        entity_type="legal",
        source="dev012_correction_test",
    )
    session.add(company)
    session.flush()
    matched = int(mode in {"positive", "zero"})
    unmatched = int(mode == "negative")
    conflicts = int(mode == "conflict")
    quarantined = int(mode == "quarantine")
    coverage = _coverage(
        matched=matched,
        unmatched=unmatched,
        conflicts=conflicts,
        quarantined=quarantined,
    )
    counters = _counters(coverage)
    dataset = _ensure_s02_dataset(session, coverage)
    run = _add_worker_run(session, counters, suffix=mode)
    artifact = _add_artifact(session, dataset, run, suffix=uuid4().int % 10**12)
    if mode == "negative":
        _add_normalized(
            session,
            dataset,
            artifact,
            inn=OTHER_INN,
            match_state="unmatched",
            suffix=1,
        )
    elif mode == "conflict":
        _add_normalized(
            session,
            dataset,
            artifact,
            inn=TARGET_INN,
            match_state="conflict",
            suffix=2,
        )
    elif mode == "quarantine":
        _add_quarantine(
            session,
            dataset,
            artifact,
            inn=quarantine_inn,
            suffix=3,
        )
    elif mode in {"positive", "zero"}:
        amount = Decimal("125.00") if mode == "positive" else Decimal("0")
        record = _add_normalized(
            session,
            dataset,
            artifact,
            inn=TARGET_INN,
            match_state="matched",
            company_id=company.id,
            amount=amount,
            suffix=4,
        )
        _add_snapshot(session, dataset, record, company, amount=amount)
    validation = {
        "coverage": coverage,
        "fact_code": "tax.debt.amount_as_of_date",
        "data_date": DATA_AS_OF.isoformat(),
        "raw_pointer": artifact.artifact_reference,
        "fact_generation": 0,
        "query_generation": 0,
    }
    pointer = session.get(WorkerPublicationState, SOURCE_ID)
    if pointer is None:
        pointer = WorkerPublicationState(source_id=SOURCE_ID)
        session.add(pointer)
    pointer.active_pointer = f"file:///private/internal/staging-{artifact.id}.json"
    pointer.rollback_pointer = None
    pointer.generation = 1
    pointer.last_fencing_token = 1
    pointer.published_by_run_id = run.id
    pointer.validation_metadata = {
        "checksum": artifact.sha256,
        "validation": validation,
        "staging": {"replayable": True, "source_id": SOURCE_ID},
    }
    pointer.updated_at = RETRIEVED_AT
    session.flush()
    return company, dataset, artifact, run


def _tax_check(risk):
    return next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )


def _assert_unresolved(result, *, reason: str):
    fact, risk, summary, projection = result
    check = _tax_check(risk)
    assert fact.state == S02FactState.SOURCE_UNAVAILABLE
    assert fact.freshness.reason == reason
    assert check.observation == Observation.UNKNOWN
    assert check.negative_closure_proven is False
    assert any(item.blocks_positive_conclusion for item in check.limitations)
    assert projection.coverage.negative_closure_proven is False
    assert projection.coverage.resolution_state == ResolutionState.UNRESOLVED
    assert all(item.factor_code != "TAX_DEBT_PRESENT" for item in risk.factors)
    assert summary.overall_conclusion.result == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
    rendered = str(projection.model_dump(mode="json"))
    assert "file:///private/internal" not in rendered
    assert "internal-worker-run" not in rendered


def test_persisted_complete_publication_proves_negative_closure(s02_db):
    company, _, _, _ = _seed_baseline(s02_db)

    fact, risk, _, projection = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )
    check = _tax_check(risk)

    assert fact.state == S02FactState.NOT_FOUND
    assert check.observation == Observation.NOT_FOUND
    assert check.negative_closure_proven is True
    assert projection.coverage.negative_closure_proven is True


def test_persisted_target_quarantine_fails_closed(s02_db):
    company, _, _, _ = _seed_baseline(
        s02_db, mode="quarantine", quarantine_inn=TARGET_INN
    )

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="target_in_quarantine")


def test_persisted_ambiguous_quarantine_fails_closed(s02_db):
    company, _, _, _ = _seed_baseline(
        s02_db, mode="quarantine", quarantine_inn=None
    )

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="publication_quarantine_identity_ambiguous")


def test_persisted_target_entity_conflict_fails_closed(s02_db):
    company, _, _, _ = _seed_baseline(s02_db, mode="conflict")

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="target_identity_conflict")


def test_persisted_incomplete_publication_counters_fail_closed(s02_db):
    company, dataset, _, _ = _seed_baseline(s02_db)
    dataset.coverage = {**dataset.coverage, "projected_facts": 1}
    pointer = s02_db.get(WorkerPublicationState, SOURCE_ID)
    pointer.validation_metadata = {
        **pointer.validation_metadata,
        "validation": {
            **pointer.validation_metadata["validation"],
            "coverage": dataset.coverage,
        },
    }
    s02_db.flush()

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="publication_processing_incomplete")


def test_persisted_dataset_error_fails_closed(s02_db):
    company, dataset, _, _ = _seed_baseline(s02_db)
    dataset.operational_status = "error"
    dataset.last_error = "current source failure"
    s02_db.flush()

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="publication_operational_state_invalid")


def test_persisted_current_last_error_fails_closed(s02_db):
    company, dataset, _, _ = _seed_baseline(s02_db)
    dataset.last_error = "current failed source state"
    s02_db.flush()

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="publication_source_error")


@pytest.mark.parametrize(
    ("mode", "amount", "has_factor"),
    (("positive", Decimal("125.00"), True), ("zero", Decimal("0"), False)),
)
def test_persisted_found_rows_preserve_positive_and_zero_semantics(
    s02_db, mode, amount, has_factor
):
    company, _, _, _ = _seed_baseline(s02_db, mode=mode)

    fact, risk, _, projection = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )
    check = _tax_check(risk)

    assert fact.state == S02FactState.FOUND
    assert fact.amount == amount
    assert (
        any(item.factor_code == "TAX_DEBT_PRESENT" for item in risk.factors)
        is has_factor
    )
    assert bool(projection.risk.factors) is has_factor
    if amount == 0:
        assert check.observation == Observation.NOT_FOUND
        assert check.negative_closure_proven is True


def test_persisted_stale_publication_remains_unresolved(s02_db):
    company, dataset, _, _ = _seed_baseline(s02_db)
    dataset.coverage = {**dataset.coverage, "official_actual_until": "2026-09-20"}
    s02_db.flush()

    fact, risk, summary, projection = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )
    check = _tax_check(risk)

    assert fact.state == S02FactState.STALE_DATA
    assert check.observation == Observation.UNKNOWN
    assert check.negative_closure_proven is False
    assert projection.coverage.negative_closure_proven is False
    assert summary.overall_conclusion.result == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION


def test_persisted_missing_publication_remains_source_unavailable(s02_db):
    company, dataset, _, _ = _seed_baseline(s02_db)
    dataset.last_data_date = None
    s02_db.flush()

    result = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    _assert_unresolved(result, reason="dataset_not_available")


def test_old_artifact_quarantine_and_conflict_do_not_contaminate_active_generation(
    s02_db,
):
    company, dataset, _, _ = _seed_baseline(s02_db)
    old_coverage = _coverage(conflicts=1, quarantined=1)
    old_run = _add_worker_run(s02_db, _counters(old_coverage), suffix="old")
    old_artifact = _add_artifact(
        s02_db, dataset, old_run, suffix=uuid4().int % 10**12
    )
    _add_normalized(
        s02_db,
        dataset,
        old_artifact,
        inn=TARGET_INN,
        match_state="conflict",
        suffix=20,
    )
    _add_quarantine(
        s02_db, dataset, old_artifact, inn=TARGET_INN, suffix=21
    )

    fact, risk, _, projection = calculate_s02_vertical_slice_from_persisted(
        s02_db, company.id, calculated_at=NOW
    )

    assert fact.state == S02FactState.NOT_FOUND
    assert _tax_check(risk).negative_closure_proven is True
    assert projection.coverage.negative_closure_proven is True


def _generation_validation(coverage, artifact, generation):
    return {
        "coverage": coverage,
        "fact_code": "tax.debt.amount_as_of_date",
        "data_date": DATA_AS_OF.isoformat(),
        "raw_pointer": artifact.artifact_reference,
        "fact_generation": generation,
        "query_generation": generation,
    }


def _add_generation(
    session,
    dataset,
    artifact,
    run,
    coverage,
    *,
    generation: int,
    scope: str,
    status: str,
):
    row = FnsTaxDebtPublicationGeneration(
        dataset_id=dataset.id,
        artifact_id=artifact.id,
        worker_run_id=run.id,
        generation=generation,
        publication_scope=scope,
        status=status,
        staging_pointer=f"file:///private/internal/generation-{generation}.json",
        raw_pointer=artifact.artifact_reference,
        checksum=artifact.sha256,
        source_as_of=SOURCE_AS_OF,
        retrieved_at=RETRIEVED_AT,
        official_actual_until=date(2026, 9, 30),
        last_data_date=DATA_AS_OF,
        record_count=coverage["normalized_records"],
        coverage=coverage,
        counters=_counters(coverage),
        validation_metadata=_generation_validation(
            coverage, artifact, generation
        ),
        dataset_metadata={
            "last_data_date": DATA_AS_OF.isoformat(),
            "source_as_of": SOURCE_AS_OF.isoformat(),
            "retrieved_at": RETRIEVED_AT.isoformat(),
            "record_count": coverage["normalized_records"],
            "coverage": coverage,
            "operational_status": "ready",
            "last_error": None,
        },
        published_at=RETRIEVED_AT,
    )
    session.add(row)
    session.flush()
    return row


def test_pilot_and_baseline_negative_closure_evidence_are_isolated(s02_db):
    cohort_company, dataset, baseline_artifact, baseline_run = _seed_baseline(
        s02_db, mode="quarantine", quarantine_inn=TARGET_INN
    )
    outside_company = Company(
        inn=OTHER_INN,
        name="DEV-012 outside cohort",
        entity_type="legal",
        source="dev012_correction_test",
    )
    s02_db.add(outside_company)
    s02_db.flush()
    baseline_coverage = dict(dataset.coverage)
    _add_generation(
        s02_db,
        dataset,
        baseline_artifact,
        baseline_run,
        baseline_coverage,
        generation=0,
        scope="baseline",
        status="baseline",
    )

    pilot_coverage = _coverage(quarantined=1)
    pilot_run = _add_worker_run(
        s02_db, _counters(pilot_coverage), suffix="pilot-generation"
    )
    pilot_artifact = _add_artifact(
        s02_db, dataset, pilot_run, suffix=uuid4().int % 10**12
    )
    _add_quarantine(
        s02_db, dataset, pilot_artifact, inn=OTHER_INN, suffix=30
    )
    _add_generation(
        s02_db,
        dataset,
        pilot_artifact,
        pilot_run,
        pilot_coverage,
        generation=1,
        scope="pilot",
        status="active",
    )
    pilot = FnsTaxDebtPilotState(
        source_id=SOURCE_ID,
        dataset_id=dataset.id,
        pilot_environment=PILOT_ENVIRONMENT,
        enabled=True,
        cohort_inns=[TARGET_INN],
        last_success_at=RETRIEVED_AT,
        active_raw_pointer=pilot_artifact.artifact_reference,
        active_checksum=pilot_artifact.sha256,
        active_source_as_of=SOURCE_AS_OF,
        active_retrieved_at=RETRIEVED_AT,
        generation=2,
        baseline_generation=0,
        normalized_generation=1,
        fact_generation=1,
        query_generation=1,
        active_data_date=DATA_AS_OF,
        baseline_data_date=DATA_AS_OF,
        counters=_counters(pilot_coverage),
        freshness="current",
        errors=[],
        updated_at=RETRIEVED_AT,
    )
    s02_db.add(pilot)
    s02_db.flush()

    cohort_result = calculate_s02_vertical_slice_from_persisted(
        s02_db, cohort_company.id, calculated_at=NOW
    )
    outside_result = calculate_s02_vertical_slice_from_persisted(
        s02_db, outside_company.id, calculated_at=NOW
    )

    assert cohort_result[0].state == S02FactState.NOT_FOUND
    assert _tax_check(cohort_result[1]).negative_closure_proven is True
    assert outside_result[0].state == S02FactState.NOT_FOUND
    assert _tax_check(outside_result[1]).negative_closure_proven is True
