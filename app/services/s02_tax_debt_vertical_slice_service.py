"""S02 persisted fact -> existing Risk/Summary -> card-data projection.

No provider, parser, worker, route, template or engine rule is invoked or
modified here.  The service only adapts persisted S02 state to the existing
Risk v3 input and projects the resulting existing Risk/Summary contracts.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import select
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
from app.models.tax_debt import CompanyTaxDebtSnapshot
from app.services.risk_engine_v3_service import calculate_risk_v3
from app.services.summary_engine_v3_service import build_summary_v3
from app.services.tax_debt_freshness import (
    evaluate_tax_debt_freshness,
    resolve_tax_debt_publication,
)
from app.sources.fns_tax_debt import (
    DATASET_CODE,
    OFFICIAL_SOURCE_PAGE,
    SOURCE_ID,
)


DEFAULT_LIMITATIONS = (
    "dated_snapshot",
    "not_real_time_balance",
    "post_publication_repayments_not_reflected",
    "legal_entities_only",
    "does_not_prove_bailiff_referral",
)


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
