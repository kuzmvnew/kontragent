"""S02 persisted fact -> existing Risk/Summary -> card-data projection.

No provider, parser, worker, route, template or engine rule is invoked or
modified here.  The service only adapts persisted S02 state to the existing
Risk v3 input and projects the resulting existing Risk/Summary contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.risk_v3 import (
    Applicability,
    Execution,
    Freshness,
    Limitation,
    NormalizedEvidenceCandidate,
    Observation,
    RiskAssessmentV3,
    ScopeCompleteness,
    SourceClass,
    SubjectIdentity,
    SubjectScope,
    TemporalKind,
    UnsupportedSubjectOutcome,
)
from app.contracts.s02_tax_debt import (
    S02CardData,
    S02FactState,
    S02PublicCoverage,
    S02PublicEvidence,
    S02PublicRisk,
    S02PublicRiskFactor,
    S02PublicSummary,
    S02SourceAttribution,
    S02TaxDebtFact,
    S02TaxDebtFreshness,
    S02TaxDebtProvenance,
)
from app.contracts.summary_v3 import SummaryV3
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import (
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
    FnsTaxDebtQuarantineRecord,
    FnsTaxDebtRawArtifact,
)
from app.models.worker import WorkerPublicationState, WorkerRun
from app.services.risk_engine_v3_service import calculate_risk_v3
from app.services.summary_engine_v3_service import build_summary_v3
from app.services.tax_debt_freshness import (
    evaluate_tax_debt_freshness,
    resolve_tax_debt_publication,
)
from app.sources.fns_tax_debt import (
    DATASET_CODE,
    OFFICIAL_SOURCE_PAGE,
    PILOT_ENVIRONMENT,
    SOURCE_ID,
)


DEFAULT_LIMITATIONS = (
    "dated_snapshot",
    "not_real_time_balance",
    "post_publication_repayments_not_reflected",
    "legal_entities_only",
    "does_not_prove_bailiff_referral",
)


@dataclass(frozen=True)
class _NegativeClosurePublication:
    artifact_id: int
    coverage: Mapping[str, Any]
    counters: Mapping[str, Any]
    record_count: int
    validation_metadata: Mapping[str, Any]
    operational_status: str | None
    last_error: str | None
    pilot_selected: bool


@dataclass(frozen=True)
class _NegativeClosureDecision:
    eligible: bool
    reason: str | None
    evidence_ref: str


_COVERAGE_COUNTERS = (
    "source_records",
    "cohort_records",
    "normalized_records",
    "quarantined_records",
    "identical_duplicates",
    "identity_conflicts",
    "matched_records",
    "unmatched_records",
    "entity_conflicts",
    "database_duplicates",
    "projected_facts",
)
_WORKER_COUNTERS = (
    "records_seen",
    "records_written",
    "records_rejected",
    "records_duplicated",
    "records_published",
)


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _quarantine_legal_entity_inn(payload: Mapping[str, Any]) -> str | None:
    """Return an INN only when the persisted payload identifies it unambiguously."""

    values: set[str] = set()
    direct = payload.get("inn")
    if direct is not None:
        values.add(str(direct).strip())
    taxpayer = payload.get("taxpayer")
    if isinstance(taxpayer, Mapping):
        for key in ("ИННЮЛ", "ИНН"):
            if taxpayer.get(key) is not None:
                values.add(str(taxpayer[key]).strip())
    valid = {value for value in values if len(value) == 10 and value.isdigit()}
    return next(iter(valid)) if len(valid) == 1 and values == valid else None


def _selected_negative_closure_publication(
    session: Session,
    *,
    dataset: DataSet,
    publication,
    inn: str,
) -> tuple[_NegativeClosurePublication | None, str | None]:
    """Resolve completeness metadata for exactly the generation being queried."""

    pilot = session.get(FnsTaxDebtPilotState, SOURCE_ID)
    pilot_active = bool(
        pilot is not None
        and pilot.enabled
        and pilot.pilot_environment == PILOT_ENVIRONMENT
        and pilot.dataset_id == dataset.id
    )
    pilot_selected = bool(
        pilot_active
        and inn in frozenset(str(value) for value in (pilot.cohort_inns or ()))
    )
    generation = session.scalar(
        select(FnsTaxDebtPublicationGeneration).where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.generation
            == publication.publication_generation,
        )
    )
    if generation is not None:
        expected_scope = "pilot" if pilot_selected and generation.generation != 0 else "baseline"
        allowed_statuses = {"active"} if pilot_selected else {"baseline", "active"}
        if (
            generation.publication_scope != expected_scope
            or generation.status not in allowed_statuses
            or generation.last_data_date != publication.data_as_of
            or generation.source_as_of != publication.source_as_of
            or generation.retrieved_at != publication.retrieved_at
        ):
            return None, "publication_generation_metadata_mismatch"
        artifact = session.get(FnsTaxDebtRawArtifact, generation.artifact_id)
        if (
            artifact is None
            or artifact.dataset_id != dataset.id
            or artifact.artifact_reference != generation.raw_pointer
            or artifact.sha256 != generation.checksum
            or artifact.source_as_of != generation.source_as_of
            or artifact.retrieved_at != generation.retrieved_at
        ):
            return None, "publication_artifact_unavailable"
        run = session.get(WorkerRun, generation.worker_run_id)
        if run is None or run.status != "succeeded":
            return None, "publication_artifact_unavailable"
        metadata = dict(generation.dataset_metadata or {})
        if pilot_selected and pilot is not None and list(pilot.errors or []):
            return None, "publication_source_error"
        metadata_status = str(metadata.get("operational_status") or "") or None
        metadata_error = str(metadata.get("last_error") or "") or None
        if metadata_status not in {"ready", "current"}:
            return None, "publication_operational_state_invalid"
        if metadata_error:
            return None, "publication_source_error"
        operational_status = (
            metadata_status if pilot_selected else dataset.operational_status
        )
        last_error = (
            metadata_error if pilot_selected else dataset.last_error
        )
        return (
            _NegativeClosurePublication(
                artifact_id=artifact.id,
                coverage=dict(generation.coverage or {}),
                counters=dict(generation.counters or {}),
                record_count=generation.record_count,
                validation_metadata=dict(generation.validation_metadata or {}),
                operational_status=operational_status,
                last_error=last_error,
                pilot_selected=pilot_selected,
            ),
            None,
        )

    # A controlled-live selection always has an immutable generation ledger.
    # Falling back to the shared baseline pointer here would mix scopes.
    if pilot_active or publication.publication_generation != 0:
        return None, "publication_generation_metadata_missing"

    pointer = session.get(WorkerPublicationState, SOURCE_ID)
    if (
        pointer is None
        or pointer.active_pointer is None
        or pointer.published_by_run_id is None
    ):
        return None, "publication_generation_metadata_missing"
    pointer_metadata = dict(pointer.validation_metadata or {})
    validation = dict(pointer_metadata.get("validation") or {})
    raw_pointer = str(validation.get("raw_pointer") or "")
    checksum = str(pointer_metadata.get("checksum") or "")
    if not raw_pointer or not checksum:
        return None, "publication_artifact_unavailable"
    artifact = session.scalar(
        select(FnsTaxDebtRawArtifact).where(
            FnsTaxDebtRawArtifact.dataset_id == dataset.id,
            FnsTaxDebtRawArtifact.artifact_reference == raw_pointer,
            FnsTaxDebtRawArtifact.sha256 == checksum,
        )
    )
    run = session.get(WorkerRun, pointer.published_by_run_id)
    if (
        artifact is None
        or artifact.source_as_of != publication.source_as_of
        or artifact.retrieved_at != publication.retrieved_at
        or run is None
        or run.status != "succeeded"
    ):
        return None, "publication_artifact_unavailable"
    counters = {name: getattr(run, name) for name in _WORKER_COUNTERS}
    return (
        _NegativeClosurePublication(
            artifact_id=artifact.id,
            coverage=dict(dataset.coverage or {}),
            counters=counters,
            record_count=dataset.record_count,
            validation_metadata=validation,
            operational_status=dataset.operational_status,
            last_error=dataset.last_error,
            pilot_selected=False,
        ),
        None,
    )


def _negative_closure_decision(
    session: Session,
    *,
    dataset: DataSet,
    publication,
    inn: str,
) -> _NegativeClosureDecision:
    selected, reason = _selected_negative_closure_publication(
        session,
        dataset=dataset,
        publication=publication,
        inn=inn,
    )
    generation_ref = (
        f"data_sets:{dataset.id}:generation:{publication.publication_generation}"
    )
    if selected is None:
        return _NegativeClosureDecision(False, reason, generation_ref)

    if selected.operational_status not in {"ready", "current"}:
        return _NegativeClosureDecision(
            False, "publication_operational_state_invalid", generation_ref
        )
    if selected.last_error:
        return _NegativeClosureDecision(
            False, "publication_source_error", generation_ref
        )

    coverage = {
        name: _non_negative_integer(selected.coverage.get(name))
        for name in _COVERAGE_COUNTERS
    }
    counters = {
        name: _non_negative_integer(selected.counters.get(name))
        for name in _WORKER_COUNTERS
    }
    if (
        any(value is None for value in coverage.values())
        or any(value is None for value in counters.values())
        or _non_negative_integer(selected.record_count) is None
    ):
        return _NegativeClosureDecision(
            False, "publication_processing_metadata_incomplete", generation_ref
        )

    validation_coverage = selected.validation_metadata.get("coverage")
    validation_data_date = selected.validation_metadata.get("data_date")
    validation_fact_generation = selected.validation_metadata.get("fact_generation")
    validation_query_generation = selected.validation_metadata.get("query_generation")
    if (
        not isinstance(validation_coverage, Mapping)
        or any(
            validation_coverage.get(name) != selected.coverage.get(name)
            for name in _COVERAGE_COUNTERS
        )
        or validation_data_date
        != (publication.data_as_of.isoformat() if publication.data_as_of else None)
        or validation_fact_generation != publication.publication_generation
        or validation_query_generation != publication.publication_generation
    ):
        return _NegativeClosureDecision(
            False, "publication_validation_metadata_mismatch", generation_ref
        )

    source_records = coverage["source_records"]
    cohort_records = coverage["cohort_records"]
    normalized_records = coverage["normalized_records"]
    quarantined_records = coverage["quarantined_records"]
    identical_duplicates = coverage["identical_duplicates"]
    identity_conflicts = coverage["identity_conflicts"]
    matched_records = coverage["matched_records"]
    unmatched_records = coverage["unmatched_records"]
    entity_conflicts = coverage["entity_conflicts"]
    database_duplicates = coverage["database_duplicates"]
    projected_facts = coverage["projected_facts"]
    assert all(
        value is not None
        for value in (
            source_records,
            cohort_records,
            normalized_records,
            quarantined_records,
            identical_duplicates,
            identity_conflicts,
            matched_records,
            unmatched_records,
            entity_conflicts,
            database_duplicates,
            projected_facts,
        )
    )

    actual_match_counts = dict(
        session.execute(
            select(
                FnsTaxDebtNormalizedRecord.match_state,
                func.count(FnsTaxDebtNormalizedRecord.id),
            )
            .where(
                FnsTaxDebtNormalizedRecord.dataset_id == dataset.id,
                FnsTaxDebtNormalizedRecord.artifact_id == selected.artifact_id,
            )
            .group_by(FnsTaxDebtNormalizedRecord.match_state)
        ).all()
    )
    actual_normalized = sum(actual_match_counts.values())
    actual_wrong_date = session.scalar(
        select(func.count(FnsTaxDebtNormalizedRecord.id)).where(
            FnsTaxDebtNormalizedRecord.dataset_id == dataset.id,
            FnsTaxDebtNormalizedRecord.artifact_id == selected.artifact_id,
            FnsTaxDebtNormalizedRecord.data_date != publication.data_as_of,
        )
    )
    actual_quarantined = session.scalar(
        select(func.count(FnsTaxDebtQuarantineRecord.id)).where(
            FnsTaxDebtQuarantineRecord.dataset_id == dataset.id,
            FnsTaxDebtQuarantineRecord.artifact_id == selected.artifact_id,
        )
    )
    actual_projected = session.scalar(
        select(func.count(CompanyTaxDebtSnapshot.id))
        .join(
            FnsTaxDebtNormalizedRecord,
            CompanyTaxDebtSnapshot.normalized_record_id
            == FnsTaxDebtNormalizedRecord.id,
        )
        .where(
            CompanyTaxDebtSnapshot.dataset_id == dataset.id,
            CompanyTaxDebtSnapshot.data_date == publication.data_as_of,
            CompanyTaxDebtSnapshot.publication_generation
            == publication.publication_generation,
            FnsTaxDebtNormalizedRecord.artifact_id == selected.artifact_id,
        )
    )
    complete = (
        source_records >= cohort_records
        and (selected.pilot_selected or source_records == cohort_records)
        and cohort_records
        == normalized_records + quarantined_records + identical_duplicates
        and normalized_records
        == matched_records + unmatched_records + entity_conflicts
        and projected_facts == matched_records
        and identity_conflicts <= quarantined_records
        and selected.record_count == normalized_records
        and counters["records_seen"] == source_records
        and counters["records_written"] == normalized_records
        and counters["records_rejected"] == quarantined_records
        and counters["records_duplicated"]
        == identical_duplicates + database_duplicates
        and counters["records_published"] == projected_facts
        and actual_normalized == normalized_records
        and actual_wrong_date == 0
        and actual_quarantined == quarantined_records
        and actual_match_counts.get("matched", 0) == matched_records
        and actual_match_counts.get("unmatched", 0) == unmatched_records
        and actual_match_counts.get("conflict", 0) == entity_conflicts
        and actual_projected == projected_facts
    )
    if not complete:
        return _NegativeClosureDecision(
            False, "publication_processing_incomplete", generation_ref
        )

    target_conflict = session.scalar(
        select(func.count(FnsTaxDebtNormalizedRecord.id)).where(
            FnsTaxDebtNormalizedRecord.dataset_id == dataset.id,
            FnsTaxDebtNormalizedRecord.artifact_id == selected.artifact_id,
            FnsTaxDebtNormalizedRecord.inn == inn,
            FnsTaxDebtNormalizedRecord.match_state == "conflict",
        )
    )
    if target_conflict:
        return _NegativeClosureDecision(
            False, "target_identity_conflict", generation_ref
        )

    quarantines = session.scalars(
        select(FnsTaxDebtQuarantineRecord).where(
            FnsTaxDebtQuarantineRecord.dataset_id == dataset.id,
            FnsTaxDebtQuarantineRecord.artifact_id == selected.artifact_id,
        )
    )
    for quarantined in quarantines:
        quarantined_inn = _quarantine_legal_entity_inn(
            dict(quarantined.raw_payload or {})
        )
        if quarantined_inn is None:
            return _NegativeClosureDecision(
                False, "publication_quarantine_identity_ambiguous", generation_ref
            )
        if quarantined_inn == inn:
            return _NegativeClosureDecision(
                False, "target_in_quarantine", generation_ref
            )

    return _NegativeClosureDecision(True, None, generation_ref)


def _timestamp(value: datetime | date | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _provenance(
    raw: Mapping[str, Any] | None,
    *,
    source_as_of: datetime | None,
    retrieved_at: datetime | None,
    source_document_id: str | None = None,
) -> S02TaxDebtProvenance:
    values = dict(raw or {})
    values.update(
        {
            "source_id": SOURCE_ID,
            "dataset_code": DATASET_CODE,
            "official_source": OFFICIAL_SOURCE_PAGE,
            "source_as_of": values.get("source_as_of") or source_as_of,
            "retrieved_at": values.get("retrieved_at") or retrieved_at,
            "source_document_id": (
                values.get("source_document_id") or source_document_id
            ),
        }
    )
    allowed = set(S02TaxDebtProvenance.model_fields)
    return S02TaxDebtProvenance.model_validate(
        {key: value for key, value in values.items() if key in allowed}
    )


def build_s02_tax_debt_fact(
    *,
    company_id: int,
    state: S02FactState,
    checked_at: datetime,
    evidence_refs: tuple[str, ...],
    amount: Decimal | None = None,
    amount_as_of_date: date | None = None,
    total_arrears: Decimal | None = None,
    total_penalties: Decimal | None = None,
    total_fines: Decimal | None = None,
    source_as_of: datetime | None = None,
    retrieved_at: datetime | None = None,
    official_actual_until: date | None = None,
    freshness_reason: str | None = None,
    source_reference: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    source_document_id: str | None = None,
    limitations: tuple[str, ...] = (),
) -> S02TaxDebtFact:
    """Create one canonical S02 fact/outcome without interpreting risk."""

    freshness_status = {
        S02FactState.FOUND: Freshness.CURRENT,
        S02FactState.NOT_FOUND: Freshness.CURRENT,
        S02FactState.STALE_DATA: Freshness.STALE,
        S02FactState.SOURCE_UNAVAILABLE: Freshness.UNKNOWN,
    }[state]
    fact_ref = ":".join(
        (
            "s02-tax-debt",
            str(company_id),
            amount_as_of_date.isoformat() if amount_as_of_date else state.value.lower(),
        )
    )
    public_limitations = _unique(
        (*DEFAULT_LIMITATIONS, *limitations)
        if state in {S02FactState.FOUND, S02FactState.NOT_FOUND}
        else (*limitations, freshness_reason or state.value.lower())
    )
    return S02TaxDebtFact(
        fact_ref=fact_ref,
        company_id=company_id,
        state=state,
        amount=amount,
        amount_as_of_date=amount_as_of_date,
        total_arrears=total_arrears,
        total_penalties=total_penalties,
        total_fines=total_fines,
        has_debt=(amount > 0 if state == S02FactState.FOUND and amount is not None else False)
        if state in {S02FactState.FOUND, S02FactState.NOT_FOUND}
        else None,
        source=S02SourceAttribution(source_reference=source_reference),
        freshness=S02TaxDebtFreshness(
            status=freshness_status,
            data_as_of=amount_as_of_date,
            source_as_of=source_as_of,
            retrieved_at=retrieved_at,
            checked_at=checked_at,
            official_actual_until=official_actual_until,
            reason=freshness_reason,
        ),
        provenance=_provenance(
            provenance,
            source_as_of=source_as_of,
            retrieved_at=retrieved_at,
            source_document_id=source_document_id,
        ),
        evidence_refs=evidence_refs,
        limitations=public_limitations,
    )


def load_s02_tax_debt_fact(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    *,
    captured_at: datetime,
) -> S02TaxDebtFact:
    """Load only persisted S02 state and fail closed on missing/stale data."""

    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must include a timezone")
    inn = str(company.inn or "").strip()
    if len(inn) != 10 or not inn.isdigit():
        return build_s02_tax_debt_fact(
            company_id=company.id,
            state=S02FactState.SOURCE_UNAVAILABLE,
            checked_at=captured_at,
            evidence_refs=(f"company:{company.id}:invalid-inn",),
            freshness_reason="legal_entity_inn_invalid",
            limitations=("legal_entity_inn_invalid",),
        )
    if dataset is None:
        return build_s02_tax_debt_fact(
            company_id=company.id,
            state=S02FactState.SOURCE_UNAVAILABLE,
            checked_at=captured_at,
            evidence_refs=("persisted-source-state:fns_tax_debt",),
            freshness_reason="dataset_not_available",
            limitations=("dataset_not_available",),
        )

    publication = resolve_tax_debt_publication(
        session,
        dataset=dataset,
        inn=inn,
    )
    if publication.data_as_of is None:
        return build_s02_tax_debt_fact(
            company_id=company.id,
            state=S02FactState.SOURCE_UNAVAILABLE,
            checked_at=captured_at,
            evidence_refs=(f"data_sets:{dataset.id}:unavailable",),
            source_as_of=publication.source_as_of,
            retrieved_at=publication.retrieved_at,
            official_actual_until=publication.official_actual_until,
            freshness_reason="dataset_not_available",
            limitations=("dataset_not_available",),
        )

    freshness = evaluate_tax_debt_freshness(publication, checked_at=captured_at)
    if not freshness.is_fresh:
        return build_s02_tax_debt_fact(
            company_id=company.id,
            state=S02FactState.STALE_DATA,
            checked_at=captured_at,
            evidence_refs=(
                f"data_sets:{dataset.id}:stale:{publication.data_as_of}",
            ),
            amount_as_of_date=publication.data_as_of,
            source_as_of=publication.source_as_of,
            retrieved_at=publication.retrieved_at,
            official_actual_until=publication.official_actual_until,
            freshness_reason=freshness.reason,
            limitations=_unique(("stale_data", freshness.reason or "stale_data")),
        )

    row = session.scalar(
        select(CompanyTaxDebtSnapshot)
        .where(
            CompanyTaxDebtSnapshot.company_id == company.id,
            CompanyTaxDebtSnapshot.dataset_id == dataset.id,
            CompanyTaxDebtSnapshot.data_date == publication.data_as_of,
            CompanyTaxDebtSnapshot.publication_generation
            == publication.publication_generation,
        )
        .order_by(CompanyTaxDebtSnapshot.id.desc())
        .limit(1)
    )
    if row is None:
        negative_closure = _negative_closure_decision(
            session,
            dataset=dataset,
            publication=publication,
            inn=inn,
        )
        if not negative_closure.eligible:
            reason = negative_closure.reason or "negative_closure_not_proven"
            return build_s02_tax_debt_fact(
                company_id=company.id,
                state=S02FactState.SOURCE_UNAVAILABLE,
                checked_at=captured_at,
                evidence_refs=(negative_closure.evidence_ref,),
                amount_as_of_date=publication.data_as_of,
                source_as_of=publication.source_as_of,
                retrieved_at=publication.retrieved_at,
                official_actual_until=publication.official_actual_until,
                freshness_reason=reason,
                limitations=(reason, "negative_closure_not_proven"),
            )
        return build_s02_tax_debt_fact(
            company_id=company.id,
            state=S02FactState.NOT_FOUND,
            checked_at=captured_at,
            evidence_refs=(
                f"data_sets:{dataset.id}:absence:{publication.data_as_of}",
            ),
            amount_as_of_date=publication.data_as_of,
            source_as_of=publication.source_as_of,
            retrieved_at=publication.retrieved_at,
            official_actual_until=publication.official_actual_until,
            limitations=("absence_is_limited_to_current_snapshot",),
        )

    amount = Decimal(row.total_debt or 0)
    return build_s02_tax_debt_fact(
        company_id=company.id,
        state=S02FactState.FOUND,
        checked_at=captured_at,
        evidence_refs=(f"company_tax_debt_snapshots:{row.id}",),
        amount=amount,
        amount_as_of_date=row.data_date,
        total_arrears=Decimal(row.total_arrears or 0),
        total_penalties=Decimal(row.total_penalties or 0),
        total_fines=Decimal(row.total_fines or 0),
        source_as_of=publication.source_as_of,
        retrieved_at=publication.retrieved_at,
        official_actual_until=publication.official_actual_until,
        source_reference=row.source_reference,
        provenance=dict(row.provenance or {}),
        source_document_id=row.source_document_id,
        limitations=tuple(row.limitation_states or ()),
    )


def _risk_limitation(fact: S02TaxDebtFact) -> tuple[Limitation, ...]:
    if fact.state not in {
        S02FactState.STALE_DATA,
        S02FactState.SOURCE_UNAVAILABLE,
    }:
        return ()
    code = (
        "STALE_DATA"
        if fact.state == S02FactState.STALE_DATA
        else "SOURCE_UNAVAILABLE"
    )
    return (
        Limitation(
            limitation_code=code,
            origin_check_ref=fact.fact_ref,
            parameters={"reason": fact.freshness.reason},
            blocks_positive_conclusion=True,
            evidence_refs=fact.evidence_refs,
        ),
    )


def s02_tax_debt_fact_to_risk_candidate(
    fact: S02TaxDebtFact,
    *,
    inn: str,
    ogrn: str | None = None,
) -> NormalizedEvidenceCandidate:
    """Adapt the S02 fact to Risk v3 input; no Risk rule is duplicated here."""

    adverse = fact.state == S02FactState.FOUND and fact.has_debt is True
    completed = fact.state in {S02FactState.FOUND, S02FactState.NOT_FOUND}
    observation = (
        Observation.FOUND
        if adverse
        else Observation.NOT_FOUND
        if completed
        else Observation.UNKNOWN
    )
    execution = (
        Execution.SOURCE_UNAVAILABLE
        if fact.state == S02FactState.SOURCE_UNAVAILABLE
        else Execution.CHECKED
    )
    scope = (
        ScopeCompleteness.UNKNOWN
        if fact.state == S02FactState.SOURCE_UNAVAILABLE
        else ScopeCompleteness.COMPLETE
    )
    fact_payload = {}
    if adverse:
        fact_payload = {
            "adverse": True,
            "fact_code": fact.fact_code,
            "total_debt": str(fact.amount),
            "total_arrears": str(fact.total_arrears),
            "total_penalties": str(fact.total_penalties),
            "total_fines": str(fact.total_fines),
            "data_date": fact.amount_as_of_date.isoformat(),
            "source_id": fact.source.source_id,
            "confirmed_positive_facts": (
                {
                    "fact_code": fact.fact_code,
                    "parameters": {
                        "amount": str(fact.amount),
                        "amount_as_of_date": fact.amount_as_of_date.isoformat(),
                        "source_id": fact.source.source_id,
                    },
                },
            ),
        }
    return NormalizedEvidenceCandidate(
        candidate_ref=fact.fact_ref,
        capability_code="tax_debt",
        fact_identity="tax.current_debt",
        company_id=fact.company_id,
        subject_identity=SubjectIdentity(
            company_id=fact.company_id,
            inn=inn,
            ogrn=ogrn or None,
        ),
        source_code=fact.source.source_code,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=fact.evidence_refs,
        exact_identity_match=True,
        applicability=Applicability.APPLICABLE,
        observation=observation,
        execution=execution,
        freshness=fact.freshness.status,
        scope=scope,
        scope_details={"s02_fact": fact.model_dump(mode="json")},
        temporal_kind=TemporalKind.CURRENT_STATE,
        source_as_of=fact.freshness.source_as_of,
        effective_at=_timestamp(fact.amount_as_of_date),
        retrieved_at=fact.freshness.retrieved_at,
        checked_at=fact.freshness.checked_at,
        negative_closure_capable=completed and not adverse,
        fact_payload=fact_payload,
        limitations=_risk_limitation(fact),
    )


def s02_tax_debt_fact_from_risk_candidate(
    candidate: NormalizedEvidenceCandidate,
) -> S02TaxDebtFact:
    if candidate.capability_code != "tax_debt":
        raise ValueError("Expected the S02 tax_debt Risk candidate")
    payload = candidate.scope_details.get("s02_fact")
    if not isinstance(payload, Mapping):
        raise ValueError("Risk candidate does not carry the canonical S02 fact")
    return S02TaxDebtFact.model_validate(payload)


def build_s02_public_projection(
    fact: S02TaxDebtFact,
    assessment: RiskAssessmentV3,
    summary: SummaryV3,
) -> S02CardData:
    """Create a safe public/card projection from existing engine results."""

    if (
        assessment.company_id != fact.company_id
        or summary.company_id != fact.company_id
    ):
        raise ValueError("S02 fact, Risk and Summary must belong to one company")
    if summary.risk_assessment_id != assessment.assessment_id:
        raise ValueError("Summary must project the supplied Risk assessment")
    check = next(
        item for item in assessment.resolved_checks if item.capability_code == "tax_debt"
    )
    factors = tuple(
        S02PublicRiskFactor(
            factor_ref=item.factor_ref,
            factor_code=item.factor_code,
            severity=item.severity,
            recommendation_code=item.recommendation_code,
            parameters=item.parameters,
        )
        for item in assessment.factors
        if item.underlying_check_ref == check.check_ref
    )
    summary_factor_refs = {item.factor_ref for item in assessment.factors}
    confirmed = tuple(
        item.fact_code
        for item in summary.confirmed_positive_facts
        if item.origin_check_ref == check.check_ref
    )
    recommendation_codes = tuple(
        item.recommendation_code
        for item in summary.recommendations
        if item.origin_ref == check.check_ref
        or item.origin_ref in {factor.factor_ref for factor in factors}
    )
    # Public provenance is intentionally bounded.  RAW paths, worker ids,
    # record hashes and checksums remain internal evidence only.
    evidence = S02PublicEvidence(
        fact_ref=fact.fact_ref,
        state=fact.state,
        amount=fact.amount,
        amount_as_of_date=fact.amount_as_of_date,
        total_arrears=fact.total_arrears,
        total_penalties=fact.total_penalties,
        total_fines=fact.total_fines,
        has_debt=fact.has_debt,
        source_document_id=fact.provenance.source_document_id,
        matching_method=fact.provenance.matching_method,
    )
    limitations = _unique(
        (
            *fact.limitations,
            *(item.limitation_code for item in check.limitations),
        )
    )
    return S02CardData(
        company_id=fact.company_id,
        summary=S02PublicSummary(
            summary_id=summary.summary_id,
            conclusion=summary.overall_conclusion.result,
            wording_key=summary.overall_conclusion.wording_key,
            key_reason_refs=tuple(
                ref for ref in summary.key_reason_refs if ref in summary_factor_refs
            ),
            confirmed_fact_codes=confirmed,
            recommendation_codes=recommendation_codes,
            summary_model_version=summary.summary_model_version,
        ),
        risk=S02PublicRisk(
            assessment_id=assessment.assessment_id,
            overall_result=assessment.overall_result,
            factors=factors,
            risk_model_version=assessment.risk_model_version,
            ruleset_version=assessment.ruleset_version,
        ),
        evidence=evidence,
        coverage=S02PublicCoverage(
            resolution_state=check.resolution_state,
            observation=check.observation,
            freshness=check.freshness,
            negative_closure_proven=check.negative_closure_proven,
            risk_resolved_checks=assessment.coverage_snapshot.numerator,
            risk_applicable_checks=assessment.coverage_snapshot.denominator,
            risk_coverage_percentage=assessment.coverage_snapshot.percentage,
            mandatory_resolved_checks=(
                assessment.coverage_snapshot.mandatory_numerator
            ),
            mandatory_applicable_checks=(
                assessment.coverage_snapshot.mandatory_denominator
            ),
        ),
        freshness=fact.freshness,
        limitations=limitations,
    )


def calculate_s02_vertical_slice(
    candidates: Sequence[NormalizedEvidenceCandidate],
    *,
    company_id: int,
    calculated_at: datetime,
) -> tuple[S02TaxDebtFact, RiskAssessmentV3, SummaryV3, S02CardData]:
    """Run the bounded Fact -> existing Risk -> existing Summary -> projection path."""

    s02_candidate = next(
        item for item in candidates if item.capability_code == "tax_debt"
    )
    fact = s02_tax_debt_fact_from_risk_candidate(s02_candidate)
    assessment = calculate_risk_v3(
        candidates,
        company_id=company_id,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=calculated_at,
    )
    if isinstance(assessment, UnsupportedSubjectOutcome):
        raise ValueError("S02 vertical slice supports legal entities only")
    summary = build_summary_v3(assessment, generated_at=calculated_at)
    return (
        fact,
        assessment,
        summary,
        build_s02_public_projection(fact, assessment, summary),
    )


def calculate_s02_vertical_slice_from_persisted(
    session: Session,
    company_id: int,
    *,
    calculated_at: datetime | None = None,
) -> tuple[S02TaxDebtFact, RiskAssessmentV3, SummaryV3, S02CardData]:
    """DB-only application entry point for the complete bounded slice."""

    # Local import avoids a module cycle: the evidence boundary delegates its
    # S02 candidate construction to this module.
    from app.services.risk_v3_evidence_service import (
        load_persisted_evidence_candidates,
    )

    captured_at = calculated_at or datetime.now(timezone.utc)
    snapshot = load_persisted_evidence_candidates(
        session,
        company_id,
        captured_at=captured_at,
    )
    if snapshot.subject_scope != SubjectScope.LEGAL_ENTITY:
        raise ValueError("S02 vertical slice supports legal entities only")
    return calculate_s02_vertical_slice(
        snapshot.candidates,
        company_id=company_id,
        calculated_at=captured_at,
    )
