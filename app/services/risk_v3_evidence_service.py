"""Bounded, DB-only evidence boundary for normalized Risk v3.

Only existing ORM rows are read. This module intentionally imports no provider,
parser, ingestion, refresh, aggregator, or worker entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.risk_v3 import (
    Applicability,
    Execution,
    Freshness,
    Limitation,
    NormalizedEvidenceCandidate,
    Observation,
    ScopeCompleteness,
    SourceClass,
    SubjectIdentity,
    SubjectScope,
    TemporalKind,
)
from app.models.cbr_warning_list import CbrWarningListEntry
from app.models.company import Company, CompanyManager
from app.models.disqualified_person import DisqualifiedPersonSnapshot
from app.models.legal_event import CompanyLegalEvent
from app.models.revenue_expense import CompanyRevenueExpenseSnapshot
from app.models.source import DataSet
from app.models.stage15_checks import ArbitrationCourtCheck
from app.models.tax_debt import CompanyTaxDebtSnapshot
from app.models.tax_offence import CompanyTaxOffence


DATASET_CODES = (
    "cbr_warning_list",
    "checko_arbitration_cases",
    "fns_disqualified",
    "fns_revenue_expenses",
    "fns_tax_debt",
    "fns_tax_offence",
)
_ADVERSE_REGISTRATION_STATUSES = {
    "INACTIVE",
    "LIQUIDATED",
    "EXCLUDED",
    "TERMINATED",
    "LIQUIDATING",
}
_CLOSED_BANKRUPTCY_EVENTS = {
    "bankruptcy_procedure_terminated",
    "bankruptcy_procedure_completed",
}


@dataclass(frozen=True)
class PersistedEvidenceSnapshot:
    company_id: int
    subject_scope: SubjectScope
    captured_at: datetime
    candidates: tuple[NormalizedEvidenceCandidate, ...]


def _timestamp(value: datetime | date | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _freshness(dataset: DataSet | None) -> Freshness:
    if dataset is None:
        return Freshness.UNKNOWN
    status = str(dataset.operational_status or "").lower()
    if status in {"current", "ready", "operational"}:
        return Freshness.CURRENT
    if status in {"stale", "outdated"}:
        return Freshness.STALE
    return Freshness.UNKNOWN


def _limitation(
    code: str,
    candidate_ref: str,
    *,
    blocks: bool = True,
    parameters: dict[str, Any] | None = None,
    evidence_refs: Iterable[str] = (),
) -> Limitation:
    return Limitation(
        limitation_code=code,
        origin_check_ref=candidate_ref,
        parameters=parameters or {},
        blocks_positive_conclusion=blocks,
        evidence_refs=tuple(sorted(set(evidence_refs))),
    )


def _subject_scope(company: Company) -> SubjectScope:
    value = str(company.entity_type or "").strip().lower()
    if value in {"legal", "legal_entity", "organization"}:
        return SubjectScope.LEGAL_ENTITY
    if value in {"individual_entrepreneur", "ip", "entrepreneur"}:
        return SubjectScope.INDIVIDUAL_ENTREPRENEUR
    return SubjectScope.UNKNOWN


def _candidate(
    *,
    company: Company,
    candidate_ref: str,
    capability_code: str,
    fact_identity: str,
    source_code: str,
    source_class: SourceClass,
    evidence_refs: Iterable[str],
    applicability: Applicability = Applicability.APPLICABLE,
    observation: Observation = Observation.UNKNOWN,
    execution: Execution = Execution.CHECKED,
    freshness: Freshness = Freshness.UNKNOWN,
    scope: ScopeCompleteness = ScopeCompleteness.UNKNOWN,
    temporal_kind: TemporalKind = TemporalKind.CURRENT_STATE,
    exact_identity_match: bool = True,
    source_as_of: datetime | date | None = None,
    effective_at: datetime | date | None = None,
    retrieved_at: datetime | date | None = None,
    checked_at: datetime | date | None = None,
    negative_closure_capable: bool = False,
    fact_payload: dict[str, Any] | None = None,
    limitations: Iterable[Limitation] = (),
    scope_details: dict[str, Any] | None = None,
) -> NormalizedEvidenceCandidate:
    return NormalizedEvidenceCandidate(
        candidate_ref=candidate_ref,
        capability_code=capability_code,
        fact_identity=fact_identity,
        company_id=company.id,
        subject_identity=SubjectIdentity(
            company_id=company.id,
            inn=company.inn,
            ogrn=company.ogrn or None,
        ),
        source_code=source_code,
        source_class=source_class,
        evidence_refs=tuple(sorted(set(evidence_refs))),
        exact_identity_match=exact_identity_match,
        applicability=applicability,
        observation=observation,
        execution=execution,
        freshness=freshness,
        scope=scope,
        scope_details=scope_details or {},
        temporal_kind=temporal_kind,
        source_as_of=_timestamp(source_as_of),
        effective_at=_timestamp(effective_at),
        retrieved_at=_timestamp(retrieved_at),
        checked_at=_timestamp(checked_at),
        negative_closure_capable=negative_closure_capable,
        fact_payload=fact_payload or {},
        limitations=tuple(limitations),
    )


def _unavailable(
    company: Company,
    *,
    capability_code: str,
    fact_identity: str,
    source_code: str,
    source_class: SourceClass,
    code: str,
    captured_at: datetime,
    applicability: Applicability = Applicability.APPLICABLE,
    temporal_kind: TemporalKind = TemporalKind.CURRENT_STATE,
    execution: Execution = Execution.NOT_CHECKED,
) -> NormalizedEvidenceCandidate:
    candidate_ref = f"db:{company.id}:{capability_code}:{code.lower()}"
    evidence_ref = f"persisted-source-state:{source_code}"
    return _candidate(
        company=company,
        candidate_ref=candidate_ref,
        capability_code=capability_code,
        fact_identity=fact_identity,
        source_code=source_code,
        source_class=source_class,
        evidence_refs=(evidence_ref,),
        applicability=applicability,
        observation=Observation.UNKNOWN,
        execution=execution,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.UNKNOWN,
        temporal_kind=temporal_kind,
        checked_at=captured_at,
        limitations=(
            _limitation(code, candidate_ref, evidence_refs=(evidence_ref,)),
        ),
    )


def _dataset_dates(dataset: DataSet | None, captured_at: datetime) -> dict[str, Any]:
    if dataset is None:
        return {"checked_at": captured_at}
    return {
        "source_as_of": dataset.source_as_of or dataset.last_data_date,
        "retrieved_at": dataset.retrieved_at,
        "checked_at": dataset.checked_at or dataset.last_success_at or captured_at,
    }


def _registration_candidate(
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    status = str(company.status or "UNKNOWN").upper()
    evidence_ref = f"companies:{company.id}:registration"
    return _candidate(
        company=company,
        candidate_ref=f"db:{company.id}:registration",
        capability_code="registration",
        fact_identity="registration.current_status",
        source_code=(dataset.code if dataset else "master_registry"),
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=(evidence_ref,),
        observation=Observation.FOUND,
        freshness=_freshness(dataset),
        scope=ScopeCompleteness.COMPLETE,
        source_as_of=(dataset.source_as_of if dataset else company.master_data_date),
        retrieved_at=(dataset.retrieved_at if dataset else company.source_updated_at),
        checked_at=(dataset.checked_at if dataset else captured_at),
        fact_payload={
            "status": status,
            "adverse": status in _ADVERSE_REGISTRATION_STATUSES,
            "confirmed_positive_facts": (
                ["ACTIVE_REGISTRATION"] if status == "ACTIVE" else []
            ),
        },
    )


def _bankruptcy_candidate(
    session: Session, company: Company, captured_at: datetime
) -> NormalizedEvidenceCandidate:
    rows = tuple(
        session.scalars(
            select(CompanyLegalEvent)
            .where(
                CompanyLegalEvent.company_id == company.id,
                CompanyLegalEvent.event_type.like("bankruptcy%"),
            )
            .order_by(
                CompanyLegalEvent.event_date.desc(), CompanyLegalEvent.id.desc()
            )
            .limit(100)
        )
    )
    if not rows:
        return _unavailable(
            company,
            capability_code="bankruptcy",
            fact_identity="bankruptcy.event",
            source_code="efrsb_persisted",
            source_class=SourceClass.AUTHORIZED_BRIDGE,
            code="EFRSB_NEGATIVE_CLOSURE_UNAVAILABLE",
            captured_at=captured_at,
            temporal_kind=TemporalKind.HISTORICAL_EVENT,
        )
    latest = rows[0]
    active = latest.event_type not in _CLOSED_BANKRUPTCY_EVENTS and str(
        latest.status
    ).lower() not in {"completed", "terminated", "closed"}
    events = [
        {
            "event_type": row.event_type,
            "event_date": row.event_date.isoformat(),
            "status": row.status,
            "source_identifier": row.source_identifier,
        }
        for row in rows
    ]
    evidence_refs = tuple(f"company_legal_events:{row.id}" for row in rows)
    candidate_ref = f"db:{company.id}:bankruptcy:{latest.id}"
    return _candidate(
        company=company,
        candidate_ref=candidate_ref,
        capability_code="bankruptcy",
        fact_identity="bankruptcy.event",
        source_code="persisted_company_legal_events",
        source_class=SourceClass.AUTHORIZED_BRIDGE,
        evidence_refs=evidence_refs,
        observation=Observation.FOUND,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.PARTIAL,
        temporal_kind=TemporalKind.HISTORICAL_EVENT,
        effective_at=latest.event_date,
        retrieved_at=latest.retrieved_at,
        checked_at=latest.checked_at,
        fact_payload={"adverse": active, "events": events},
        limitations=(
            _limitation(
                "EFRSB_PRODUCTION_CLOSURE_INCOMPLETE",
                candidate_ref,
                evidence_refs=evidence_refs,
            ),
        ),
        scope_details={"loaded_event_count": len(rows), "bounded_limit": 100},
    )


def _tax_debt_candidate(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    if dataset is None or dataset.last_data_date is None:
        return _unavailable(
            company,
            capability_code="tax_debt",
            fact_identity="tax.current_debt",
            source_code="fns_tax_debt",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="DATASET_NOT_AVAILABLE",
            captured_at=captured_at,
            execution=Execution.SOURCE_UNAVAILABLE,
        )
    row = session.scalar(
        select(CompanyTaxDebtSnapshot)
        .where(
            CompanyTaxDebtSnapshot.company_id == company.id,
            CompanyTaxDebtSnapshot.dataset_id == dataset.id,
            CompanyTaxDebtSnapshot.data_date == dataset.last_data_date,
        )
        .order_by(CompanyTaxDebtSnapshot.id.desc())
        .limit(1)
    )
    debt = row.total_debt if row else Decimal("0")
    found = debt > 0
    evidence_ref = (
        f"company_tax_debt_snapshots:{row.id}"
        if row
        else f"data_sets:{dataset.id}:absence:{dataset.last_data_date}"
    )
    return _candidate(
        company=company,
        candidate_ref=f"db:{company.id}:tax_debt:{dataset.last_data_date}",
        capability_code="tax_debt",
        fact_identity="tax.current_debt",
        source_code=dataset.code,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=(evidence_ref,),
        observation=Observation.FOUND if found else Observation.NOT_FOUND,
        freshness=_freshness(dataset),
        scope=ScopeCompleteness.COMPLETE,
        negative_closure_capable=not found,
        fact_payload=(
            {
                "adverse": True,
                "total_debt": str(debt),
                "total_arrears": str(row.total_arrears),
                "total_penalties": str(row.total_penalties),
                "total_fines": str(row.total_fines),
                "data_date": row.data_date.isoformat(),
            }
            if found and row
            else {}
        ),
        **_dataset_dates(dataset, captured_at),
    )


def _tax_offence_candidate(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    if dataset is None or dataset.last_data_date is None:
        return _unavailable(
            company,
            capability_code="tax_offence",
            fact_identity="tax.offence",
            source_code="fns_tax_offence",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="DATASET_NOT_AVAILABLE",
            captured_at=captured_at,
            temporal_kind=TemporalKind.HISTORICAL_EVENT,
            execution=Execution.SOURCE_UNAVAILABLE,
        )
    rows = tuple(
        session.scalars(
            select(CompanyTaxOffence)
            .where(
                CompanyTaxOffence.company_id == company.id,
                CompanyTaxOffence.dataset_id == dataset.id,
                CompanyTaxOffence.data_date == dataset.last_data_date,
            )
            .order_by(
                CompanyTaxOffence.document_date.desc().nullslast(),
                CompanyTaxOffence.id.desc(),
            )
            .limit(100)
        )
    )
    found = bool(rows)
    evidence_refs = (
        tuple(f"company_tax_offences:{row.id}" for row in rows)
        if rows
        else (f"data_sets:{dataset.id}:absence:{dataset.last_data_date}",)
    )
    total_fine = sum((row.fine_amount for row in rows), Decimal("0"))
    return _candidate(
        company=company,
        candidate_ref=f"db:{company.id}:tax_offence:{dataset.last_data_date}",
        capability_code="tax_offence",
        fact_identity="tax.offence",
        source_code=dataset.code,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=evidence_refs,
        observation=Observation.FOUND if found else Observation.NOT_FOUND,
        freshness=_freshness(dataset),
        scope=ScopeCompleteness.COMPLETE,
        temporal_kind=TemporalKind.HISTORICAL_EVENT,
        effective_at=max(
            (row.document_date for row in rows if row.document_date),
            default=dataset.last_data_date,
        ),
        negative_closure_capable=not found,
        fact_payload=(
            {
                "adverse": True,
                "document_count": len(rows),
                "total_fine": str(total_fine),
                "data_date": dataset.last_data_date.isoformat(),
            }
            if found
            else {}
        ),
        **_dataset_dates(dataset, captured_at),
    )


def _finance_candidate(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    if dataset is None or dataset.last_data_date is None:
        return _unavailable(
            company,
            capability_code="finance",
            fact_identity="finance.period_result",
            source_code="fns_revenue_expenses",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="FINANCE_APPLICABILITY_UNKNOWN",
            captured_at=captured_at,
            applicability=Applicability.APPLICABILITY_UNKNOWN,
            temporal_kind=TemporalKind.HISTORICAL_EVENT,
        )
    row = session.scalar(
        select(CompanyRevenueExpenseSnapshot)
        .where(
            CompanyRevenueExpenseSnapshot.company_id == company.id,
            CompanyRevenueExpenseSnapshot.dataset_id == dataset.id,
        )
        .order_by(
            CompanyRevenueExpenseSnapshot.data_year.desc(),
            CompanyRevenueExpenseSnapshot.data_date.desc(),
        )
        .limit(1)
    )
    if row is None:
        return _unavailable(
            company,
            capability_code="finance",
            fact_identity="finance.period_result",
            source_code=dataset.code,
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="FINANCE_APPLICABILITY_UNKNOWN",
            captured_at=captured_at,
            applicability=Applicability.APPLICABILITY_UNKNOWN,
            temporal_kind=TemporalKind.HISTORICAL_EVENT,
        )
    return _candidate(
        company=company,
        candidate_ref=f"db:{company.id}:finance:{row.id}",
        capability_code="finance",
        fact_identity="finance.period_result",
        source_code=dataset.code,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=(f"company_revenue_expense_snapshots:{row.id}",),
        observation=Observation.FOUND,
        freshness=_freshness(dataset),
        scope=ScopeCompleteness.COMPLETE,
        temporal_kind=TemporalKind.HISTORICAL_EVENT,
        effective_at=date(row.data_year, 12, 31),
        fact_payload={
            "adverse": row.profit_loss < 0,
            "revenue": str(row.revenue),
            "expenses": str(row.expenses),
            "profit_loss": str(row.profit_loss),
            "period": str(row.data_year),
            "confirmed_positive_facts": (
                ["NON_NEGATIVE_FINANCIAL_RESULT"] if row.profit_loss >= 0 else []
            ),
        },
        **_dataset_dates(dataset, captured_at),
    )


def _case_is_defendant(case: Any, inn: str) -> bool:
    if not isinstance(case, dict):
        return False
    role = str(case.get("role") or case.get("company_role") or "").lower()
    if role in {"defendant", "ответчик"}:
        return True
    defendants = case.get("defendants") or ()
    return any(
        isinstance(item, dict) and str(item.get("inn") or "") == inn
        for item in defendants
    )


def _arbitration_candidate(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    row = session.scalar(
        select(ArbitrationCourtCheck)
        .where(ArbitrationCourtCheck.company_id == company.id)
        .order_by(
            ArbitrationCourtCheck.checked_at.desc(), ArbitrationCourtCheck.id.desc()
        )
        .limit(1)
    )
    if row is None:
        return _unavailable(
            company,
            capability_code="arbitration",
            fact_identity="arbitration.company_cases",
            source_code="checko_arbitration_cases",
            source_class=SourceClass.AUTHORIZED_BRIDGE,
            code="ARBITRATION_COVERAGE_INCOMPLETE",
            captured_at=captured_at,
            temporal_kind=TemporalKind.HISTORICAL_EVENT,
        )
    if row.result_status != "success":
        return _unavailable(
            company,
            capability_code="arbitration",
            fact_identity="arbitration.company_cases",
            source_code=row.source_url or "checko_arbitration_cases",
            source_class=SourceClass.AUTHORIZED_BRIDGE,
            code="ARBITRATION_SOURCE_UNAVAILABLE",
            captured_at=captured_at,
            temporal_kind=TemporalKind.HISTORICAL_EVENT,
            execution=Execution.SOURCE_UNAVAILABLE,
        )
    cases = list(row.cases or [])
    complete = row.total_pages is not None and row.loaded_pages >= row.total_pages
    found = bool(cases)
    evidence_ref = f"arbitration_court_checks:{row.id}"
    candidate_ref = f"db:{company.id}:arbitration:{row.id}"
    limitations = ()
    if not complete:
        limitations = (
            _limitation(
                "ARBITRATION_COVERAGE_INCOMPLETE",
                candidate_ref,
                evidence_refs=(evidence_ref,),
            ),
        )
    return _candidate(
        company=company,
        candidate_ref=candidate_ref,
        capability_code="arbitration",
        fact_identity="arbitration.company_cases",
        source_code="checko_arbitration_cases",
        source_class=SourceClass.AUTHORIZED_BRIDGE,
        evidence_refs=(evidence_ref,),
        observation=Observation.FOUND if found else Observation.NOT_FOUND,
        freshness=_freshness(dataset),
        scope=(
            ScopeCompleteness.COMPLETE if complete else ScopeCompleteness.PARTIAL
        ),
        temporal_kind=TemporalKind.HISTORICAL_EVENT,
        effective_at=row.date_to,
        checked_at=row.checked_at,
        negative_closure_capable=not found and complete,
        fact_payload=(
            {
                "adverse": any(_case_is_defendant(case, company.inn) for case in cases),
                "case_count": len(cases),
                "reported_total_count": row.total_count,
                "period": {"from": row.date_from.isoformat(), "to": row.date_to.isoformat()},
            }
            if found
            else {}
        ),
        limitations=limitations,
        scope_details={
            "loaded_pages": row.loaded_pages,
            "total_pages": row.total_pages,
            "loaded_case_count": len(cases),
            "reported_total_count": row.total_count,
        },
    )


def _cbr_warning_candidate(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    if dataset is None or dataset.last_data_date is None:
        return _unavailable(
            company,
            capability_code="cbr_warning",
            fact_identity="cbr.warning_list_match",
            source_code="cbr_warning_list",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="DATASET_NOT_AVAILABLE",
            captured_at=captured_at,
            execution=Execution.SOURCE_UNAVAILABLE,
        )
    rows = tuple(
        session.scalars(
            select(CbrWarningListEntry)
            .where(
                CbrWarningListEntry.dataset_id == dataset.id,
                CbrWarningListEntry.data_date == dataset.last_data_date,
                CbrWarningListEntry.inn == company.inn,
            )
            .order_by(CbrWarningListEntry.id)
            .limit(50)
        )
    )
    found = bool(rows)
    evidence_refs = (
        tuple(f"cbr_warning_list_entries:{row.id}" for row in rows)
        if rows
        else (f"data_sets:{dataset.id}:absence:{dataset.last_data_date}",)
    )
    return _candidate(
        company=company,
        candidate_ref=f"db:{company.id}:cbr_warning:{dataset.last_data_date}",
        capability_code="cbr_warning",
        fact_identity="cbr.warning_list_match",
        source_code=dataset.code,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=evidence_refs,
        observation=Observation.FOUND if found else Observation.NOT_FOUND,
        freshness=_freshness(dataset),
        scope=ScopeCompleteness.COMPLETE,
        negative_closure_capable=not found,
        fact_payload=(
            {
                "adverse": True,
                "record_count": len(rows),
                "cbr_ids": [row.cbr_id for row in rows],
            }
            if found
            else {}
        ),
        **_dataset_dates(dataset, captured_at),
    )


def _normalize_name(value: str | None) -> str:
    return " ".join(str(value or "").upper().split())


def _management_candidate(
    session: Session,
    company: Company,
    dataset: DataSet | None,
    captured_at: datetime,
) -> NormalizedEvidenceCandidate:
    if dataset is None or dataset.last_data_date is None:
        return _unavailable(
            company,
            capability_code="management_disqualification",
            fact_identity="management.current_disqualification",
            source_code="fns_disqualified",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="DATASET_NOT_AVAILABLE",
            captured_at=captured_at,
            execution=Execution.SOURCE_UNAVAILABLE,
        )
    managers = tuple(
        session.scalars(
            select(CompanyManager)
            .where(
                CompanyManager.company_id == company.id,
                CompanyManager.is_current.is_(True),
            )
            .order_by(CompanyManager.id)
            .limit(20)
        )
    )
    if not managers or not any(_normalize_name(item.full_name) for item in managers):
        return _unavailable(
            company,
            capability_code="management_disqualification",
            fact_identity="management.current_disqualification",
            source_code=dataset.code,
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="CURRENT_MANAGEMENT_IDENTITY_UNAVAILABLE",
            captured_at=captured_at,
        )
    records = tuple(
        session.scalars(
            select(DisqualifiedPersonSnapshot)
            .where(
                DisqualifiedPersonSnapshot.dataset_id == dataset.id,
                DisqualifiedPersonSnapshot.data_date == dataset.last_data_date,
                DisqualifiedPersonSnapshot.organization_inn == company.inn,
            )
            .order_by(DisqualifiedPersonSnapshot.id)
            .limit(100)
        )
    )
    manager_names = {_normalize_name(item.full_name) for item in managers}
    matches = tuple(
        row
        for row in records
        if _normalize_name(row.full_name) in manager_names
        and (row.start_date is None or row.start_date <= dataset.last_data_date)
        and (row.end_date is None or row.end_date >= dataset.last_data_date)
    )
    found = bool(matches)
    evidence_refs = (
        tuple(f"disqualified_person_snapshots:{row.id}" for row in matches)
        if matches
        else (
            f"data_sets:{dataset.id}:absence-for-current-managers:{dataset.last_data_date}",
        )
    )
    return _candidate(
        company=company,
        candidate_ref=f"db:{company.id}:management_disqualification:{dataset.last_data_date}",
        capability_code="management_disqualification",
        fact_identity="management.current_disqualification",
        source_code=dataset.code,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=evidence_refs,
        observation=Observation.FOUND if found else Observation.NOT_FOUND,
        freshness=_freshness(dataset),
        scope=ScopeCompleteness.COMPLETE,
        negative_closure_capable=not found,
        fact_payload=(
            {
                "adverse": True,
                "matching_method": "current_manager_full_name_exact_and_company_inn",
                "register_numbers": [row.register_number for row in matches],
            }
            if found
            else {}
        ),
        **_dataset_dates(dataset, captured_at),
    )


def load_persisted_evidence_candidates(
    session: Session,
    company_id: int,
    *,
    captured_at: datetime | None = None,
) -> PersistedEvidenceSnapshot:
    """Capture all bounded rows before calculation begins."""

    captured_at = captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must include a timezone")
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError("Company not found")
    scope = _subject_scope(company)
    if scope != SubjectScope.LEGAL_ENTITY:
        return PersistedEvidenceSnapshot(
            company_id=company.id,
            subject_scope=scope,
            captured_at=captured_at,
            candidates=(),
        )

    datasets = {
        item.code: item
        for item in session.scalars(
            select(DataSet).where(DataSet.code.in_(DATASET_CODES))
        )
    }
    master_dataset = (
        session.get(DataSet, company.master_dataset_id)
        if company.master_dataset_id is not None
        else None
    )
    candidates = [
        _registration_candidate(company, master_dataset, captured_at),
        _bankruptcy_candidate(session, company, captured_at),
        _unavailable(
            company,
            capability_code="fssp",
            fact_identity="fssp.active_enforcement",
            source_code="fssp",
            source_class=SourceClass.OFFICIAL_DIRECT,
            code="FSSP_NEGATIVE_CLOSURE_UNAVAILABLE",
            captured_at=captured_at,
            execution=Execution.NOT_CHECKED,
        ),
        _tax_debt_candidate(
            session, company, datasets.get("fns_tax_debt"), captured_at
        ),
        _tax_offence_candidate(
            session, company, datasets.get("fns_tax_offence"), captured_at
        ),
        _finance_candidate(
            session, company, datasets.get("fns_revenue_expenses"), captured_at
        ),
        _arbitration_candidate(
            session, company, datasets.get("checko_arbitration_cases"), captured_at
        ),
        _cbr_warning_candidate(
            session, company, datasets.get("cbr_warning_list"), captured_at
        ),
        _unavailable(
            company,
            capability_code="cbr_zsk",
            fact_identity="cbr.zsk_high_risk",
            source_code="cbr_zsk",
            source_class=SourceClass.OFFICIAL_DIRECT,
            code="CBR_ZSK_CHALLENGE_UNRESOLVED",
            captured_at=captured_at,
            execution=Execution.NOT_CHECKED,
        ),
        _management_candidate(
            session, company, datasets.get("fns_disqualified"), captured_at
        ),
        _unavailable(
            company,
            capability_code="licence_sro",
            fact_identity="licence_sro.required_status",
            source_code="licence_sro_persisted",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="LICENCE_SRO_APPLICABILITY_UNKNOWN",
            captured_at=captured_at,
            applicability=Applicability.APPLICABILITY_UNKNOWN,
        ),
        _unavailable(
            company,
            capability_code="bankinform",
            fact_identity="tax.account_suspension",
            source_code="bankinform",
            source_class=SourceClass.OFFICIAL_DIRECT,
            code="BANKINFORM_APPLICABILITY_UNKNOWN",
            captured_at=captured_at,
            applicability=Applicability.APPLICABILITY_UNKNOWN,
        ),
        _unavailable(
            company,
            capability_code="industry_specific",
            fact_identity="industry.required_check",
            source_code="industry_specific",
            source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
            code="INDUSTRY_APPLICABILITY_UNKNOWN",
            captured_at=captured_at,
            applicability=Applicability.APPLICABILITY_UNKNOWN,
        ),
    ]
    return PersistedEvidenceSnapshot(
        company_id=company.id,
        subject_scope=scope,
        captured_at=captured_at,
        candidates=tuple(candidates),
    )
