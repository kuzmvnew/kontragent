"""Monitoring P0 domain service.

The detector reads only accepted semantic facts.  It never reads provider
payloads, RAW artifacts, parser objects, or ingestion timestamps.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.monitoring import (
    CompanyMonitoringSnapshot,
    MonitoringEvent,
    MonitoringSubscription,
    WorkspaceFeedEntry,
)
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.semantic_fact import CompanySemanticFact
from app.models.workspace import (
    SavedCompany,
    WorkspaceAuditEvent,
    WorkspaceEntitlement,
)
from workspace_app.service import ActionDenied, action_state, authorize, resolve_legal_company


SEVERITY_POLICY_VERSION = "monitoring-severity-p0-v1"
EVENT_REGISTRY_VERSION = "monitoring-event-registry-p0-v1"
_EVENT_NAMESPACE = UUID("b4491e92-a36f-4ac6-b781-5b07b26ac14d")
_COVERAGE_STATES = frozenset(
    {
        "NOT_CHECKED",
        "SOURCE_UNAVAILABLE",
        "TIMEOUT",
        "PARSING_ERROR",
        "STALE_DATA",
        "UNKNOWN",
        "PARTIAL",
        "CONFLICTING_EVIDENCE",
    }
)
_CLIENT_RIGHTS = frozenset({"PUBLIC", "AUTHENTICATED_ONLY"})
_BUSINESS_STATES = frozenset({"FOUND", "NOT_FOUND", "NOT_APPLICABLE"})


EVENT_TITLES = {
    "COMPANY_STATUS_CHANGED": "Изменился статус компании",
    "MANAGER_CHANGED": "Изменились сведения о руководителе",
    "FOUNDER_CHANGED": "Изменились сведения об учредителе",
    "ADDRESS_CHANGED": "Изменился адрес компании",
    "TAX_DEBT_CHANGED": "Изменились сведения о налоговой задолженности",
    "ENFORCEMENT_CHANGED": "Изменились сведения об исполнительных производствах",
    "LICENSE_CHANGED": "Изменились сведения о лицензиях",
    "COURT_STATE_CHANGED": "Изменились сведения о судебных делах",
    "BANKRUPTCY_CHANGED": "Изменились сведения о банкротстве",
    "RESTRICTION_CHANGED": "Изменились сведения об ограничениях",
    "INSPECTION_CHANGED": "Изменились сведения о проверках",
    "FINANCE_CHANGED": "Изменились финансовые показатели",
    "GENERIC_FACT_CHANGED": "Изменились сведения о компании",
}

_SEVERITY_BY_EVENT = {
    "COMPANY_STATUS_CHANGED": "HIGH",
    "BANKRUPTCY_CHANGED": "HIGH",
    "RESTRICTION_CHANGED": "HIGH",
    "MANAGER_CHANGED": "MEDIUM",
    "FOUNDER_CHANGED": "MEDIUM",
    "TAX_DEBT_CHANGED": "MEDIUM",
    "ENFORCEMENT_CHANGED": "MEDIUM",
    "LICENSE_CHANGED": "MEDIUM",
    "COURT_STATE_CHANGED": "MEDIUM",
    "ADDRESS_CHANGED": "LOW",
    "INSPECTION_CHANGED": "LOW",
    "FINANCE_CHANGED": "LOW",
    "GENERIC_FACT_CHANGED": "INFO",
}


@dataclass(frozen=True)
class MonitoringState:
    state: str
    message: str
    can_manage: bool
    denial_code: str | None
    subscription_id: UUID | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    last_checked_at: datetime | None = None


@dataclass(frozen=True)
class DetectedChange:
    company_id: int
    fact_ref: str | None
    origin: str
    event_type: str
    change_kind: str
    section_key: str
    field_key: str
    period_identity: str
    item_identity: str
    source_code: str | None
    old_value: Any
    new_value: Any
    old_state: str | None
    new_state: str | None
    severity: str
    source_as_of: date | None
    evidence_refs: tuple[str, ...]
    user_visible: bool
    dedupe_key: str


@dataclass(frozen=True)
class MonitoringRunResult:
    company_id: int
    snapshot_id: UUID | None
    subscription_count: int
    detected_change_count: int
    canonical_event_count: int
    feed_entry_count: int


@dataclass(frozen=True)
class WorkspaceFeedItem:
    entry_id: UUID
    subscription_id: UUID
    event_ref: str
    company_id: int
    company_name: str
    inn: str
    title: str
    event_type: str
    change_kind: str
    old_value: Any
    new_value: Any
    old_state: str | None
    new_state: str | None
    severity: str
    detected_at: datetime
    source_code: str | None
    evidence_refs: tuple[str, ...]
    created_at: datetime
    read_at: datetime | None


def _now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("monitoring timestamps must contain a timezone")
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coordinate(fact: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(fact.get("section_key") or ""),
        str(fact.get("field_key") or ""),
        str(fact.get("period_identity") or ""),
        str(fact.get("item_identity") or ""),
    )


def _privacy_coordinate_digest(company_id: int, coordinate: tuple[str, str, str, str]) -> str:
    return _digest(
        {
            "domain": "monitoring-privacy-coordinate-v1",
            "company_id": company_id,
            "coordinate": coordinate,
        }
    )


def _current_risk_summary_refs(session: Session, company_id: int) -> tuple[str | None, str | None]:
    risk_ref = session.scalar(
        sa.select(CompanyRiskAssessmentV3.assessment_id)
        .where(CompanyRiskAssessmentV3.company_id == company_id)
        .order_by(CompanyRiskAssessmentV3.calculated_at.desc(), CompanyRiskAssessmentV3.id.desc())
        .limit(1)
    )
    summary_ref = session.scalar(
        sa.select(CompanySummaryV3.summary_id)
        .where(CompanySummaryV3.company_id == company_id)
        .order_by(CompanySummaryV3.generated_at.desc(), CompanySummaryV3.id.desc())
        .limit(1)
    )
    return risk_ref, summary_ref


def _snapshot_fact(row: CompanySemanticFact) -> dict[str, Any]:
    evidence = row.selected_evidence if isinstance(row.selected_evidence, dict) else {}
    evidence_ref = evidence.get("evidence_ref")
    return {
        "fact_ref": row.fact_ref,
        "section_key": row.section_key,
        "field_key": row.field_key,
        "period_identity": row.period_identity or "",
        "item_identity": row.item_identity or "",
        "state": row.state,
        "value": _jsonable(evidence.get("value")),
        "evidence_ref": str(evidence_ref)[:240] if evidence_ref else None,
        "source_code": str(evidence.get("source_code"))[:100] if evidence.get("source_code") else None,
        "source_class": str(evidence.get("source_class"))[:80] if evidence.get("source_class") else None,
        "source_data_date": _parse_date(evidence.get("source_data_date")).isoformat()
        if _parse_date(evidence.get("source_data_date"))
        else None,
        "rights": row.rights,
    }


def _observed_semantic_state(
    session: Session,
    *,
    company_id: int,
) -> tuple[list[dict[str, Any]], set[str], str | None, str | None]:
    if session.get(Company, company_id) is None:
        raise ValueError(f"company_id {company_id} is not resolved")
    rows = tuple(
        session.scalars(
            sa.select(CompanySemanticFact)
            .where(
                CompanySemanticFact.company_id == company_id,
                CompanySemanticFact.is_current.is_(True),
            )
            .order_by(
                CompanySemanticFact.section_key,
                CompanySemanticFact.field_key,
                CompanySemanticFact.period_identity,
                CompanySemanticFact.item_identity,
                CompanySemanticFact.fact_ref,
            )
        ).all()
    )
    facts = [_snapshot_fact(row) for row in rows if row.rights in _CLIENT_RIGHTS]
    restricted = {
        _privacy_coordinate_digest(
            company_id,
            (
                row.section_key,
                row.field_key,
                row.period_identity or "",
                row.item_identity or "",
            ),
        )
        for row in rows
        if row.rights == "INTERNAL_ONLY"
    }
    risk_ref, summary_ref = _current_risk_summary_refs(session, company_id)
    return facts, restricted, risk_ref, summary_ref


def _last_known_business_facts(
    previous: CompanyMonitoringSnapshot | None,
    observed_facts: list[dict[str, Any]],
    *,
    company_id: int,
    privacy_blocked_coordinates: set[str],
) -> list[dict[str, Any]]:
    known = {
        _coordinate(fact): fact
        for fact in (previous.last_known_business_facts if previous is not None else ())
        if _privacy_coordinate_digest(company_id, _coordinate(fact))
        not in privacy_blocked_coordinates
    }
    for fact in observed_facts:
        if fact["state"] in _BUSINESS_STATES:
            known[_coordinate(fact)] = fact
    return [known[coordinate] for coordinate in sorted(known)]


def _create_snapshot(
    session: Session,
    *,
    company_id: int,
    facts: list[dict[str, Any]],
    restricted_coordinates: set[str],
    risk_ref: str | None,
    summary_ref: str | None,
    previous_snapshot: CompanyMonitoringSnapshot | None,
    captured_at: datetime | None,
) -> CompanyMonitoringSnapshot:
    privacy_blocked = (
        set(previous_snapshot.privacy_blocked_coordinates)
        if previous_snapshot is not None
        else set()
    ) | restricted_coordinates
    # A coverage-only eligible row does not establish a business baseline.
    # Keep the barrier until an authoritative eligible value is observed.
    privacy_blocked.difference_update(
        _privacy_coordinate_digest(company_id, _coordinate(fact))
        for fact in facts
        if fact["state"] in _BUSINESS_STATES
    )
    last_known = _last_known_business_facts(
        previous_snapshot,
        facts,
        company_id=company_id,
        privacy_blocked_coordinates=privacy_blocked,
    )
    blocked_digests = sorted(privacy_blocked)
    revision_input = {
        "company_id": company_id,
        "facts": facts,
        "last_known_business_facts": last_known,
        "privacy_blocked_coordinates": blocked_digests,
        "company_view_revision": None,
        "risk_ref": risk_ref,
        "summary_ref": summary_ref,
    }
    fingerprint = _digest(revision_input)
    snapshot = CompanyMonitoringSnapshot(
        id=uuid4(),
        company_id=company_id,
        # Persisted CompanySemanticFact does not carry the materialized View
        # revision.  Keep this null instead of inventing a cv1 revision.
        company_view_revision=None,
        risk_ref=risk_ref,
        summary_ref=summary_ref,
        facts=facts,
        last_known_business_facts=last_known,
        privacy_blocked_coordinates=blocked_digests,
        fact_count=len(facts),
        fingerprint=fingerprint,
        captured_at=_now(captured_at),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def capture_snapshot(
    session: Session,
    *,
    company_id: int,
    captured_at: datetime | None = None,
    previous_snapshot: CompanyMonitoringSnapshot | None = None,
) -> CompanyMonitoringSnapshot:
    """Append observed and last-known business state in the caller transaction."""

    if previous_snapshot is not None and previous_snapshot.company_id != company_id:
        raise ValueError("previous monitoring snapshot belongs to another company")
    facts, restricted, risk_ref, summary_ref = _observed_semantic_state(
        session, company_id=company_id
    )
    return _create_snapshot(
        session,
        company_id=company_id,
        facts=facts,
        restricted_coordinates=restricted,
        risk_ref=risk_ref,
        summary_ref=summary_ref,
        previous_snapshot=previous_snapshot,
        captured_at=captured_at,
    )


def _event_type(section_key: str, field_key: str) -> str:
    identity = f"{section_key} {field_key}".upper()
    if "BANKRUPT" in identity:
        return "BANKRUPTCY_CHANGED"
    if any(token in identity for token in ("RESTRICTION", "DISQUAL", "SANCTION")):
        return "RESTRICTION_CHANGED"
    if any(token in identity for token in ("TAX_DEBT", "DEBT")) and "ENFORC" not in identity:
        return "TAX_DEBT_CHANGED"
    if any(token in identity for token in ("ENFORC", "FSSP")):
        return "ENFORCEMENT_CHANGED"
    if any(token in identity for token in ("COURT", "ARBITRATION", "CASE")):
        return "COURT_STATE_CHANGED"
    if any(token in identity for token in ("LICENSE", "LICENCE")):
        return "LICENSE_CHANGED"
    if any(token in identity for token in ("MANAGER", "DIRECTOR", "HEAD")):
        return "MANAGER_CHANGED"
    if any(token in identity for token in ("FOUNDER", "OWNER", "PARTICIPANT")):
        return "FOUNDER_CHANGED"
    if "ADDRESS" in identity:
        return "ADDRESS_CHANGED"
    if any(token in identity for token in ("INSPECTION", "ERKNM")):
        return "INSPECTION_CHANGED"
    if any(token in identity for token in ("FINANCE", "REVENUE", "PROFIT", "EQUITY", "EMPLOYEE")):
        return "FINANCE_CHANGED"
    if "STATUS" in identity:
        return "COMPANY_STATUS_CHANGED"
    return "GENERIC_FACT_CHANGED"


def _change(
    company_id: int,
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
) -> DetectedChange | None:
    fact = new or old
    assert fact is not None
    old_state = str(old.get("state")) if old else None
    new_state = str(new.get("state")) if new else None
    old_value = _jsonable(old.get("value")) if old else None
    new_value = _jsonable(new.get("value")) if new else None

    if old is None:
        change_kind = "FACT_ADDED"
    elif new is None:
        change_kind = "FACT_REMOVED"
    elif old_state != new_state:
        change_kind = "STATE_CHANGED"
    elif _canonical(old_value) != _canonical(new_value):
        change_kind = "FACT_CHANGED"
    else:
        return None

    coverage_change = (
        new is None
        or old_state in _COVERAGE_STATES
        or new_state in _COVERAGE_STATES
    )
    origin = "COVERAGE_CHANGE" if coverage_change else "SOURCE_CHANGE"
    section_key, field_key, period_identity, item_identity = _coordinate(fact)
    event_type = _event_type(section_key, field_key)
    resolved = new_state in {"NOT_FOUND", "NOT_APPLICABLE"}
    severity = "INFO" if coverage_change or resolved else _SEVERITY_BY_EVENT[event_type]
    rights = str((new or old or {}).get("rights") or "INTERNAL_ONLY")
    user_visible = origin == "SOURCE_CHANGE" and rights in _CLIENT_RIGHTS
    source_fact = new or old or {}
    evidence_refs = tuple(
        sorted(
            {
                str(candidate.get("evidence_ref"))
                for candidate in (old, new)
                if candidate and candidate.get("evidence_ref")
            }
        )
    )
    source_as_of = _parse_date(source_fact.get("source_data_date"))
    dedupe_input = {
        "company_id": company_id,
        "coordinate": [section_key, field_key, period_identity, item_identity],
        "origin": origin,
        "event_type": event_type,
        "change_kind": change_kind,
        "old_state": old_state,
        "new_state": new_state,
        "old_value": old_value,
        "new_value": new_value,
        "old_source_as_of": old.get("source_data_date") if old else None,
        "new_source_as_of": new.get("source_data_date") if new else None,
    }
    return DetectedChange(
        company_id=company_id,
        fact_ref=str(fact.get("fact_ref")) if fact.get("fact_ref") else None,
        origin=origin,
        event_type=event_type,
        change_kind=change_kind,
        section_key=section_key,
        field_key=field_key,
        period_identity=period_identity,
        item_identity=item_identity,
        source_code=str(source_fact.get("source_code")) if source_fact.get("source_code") else None,
        old_value=old_value,
        new_value=new_value,
        old_state=old_state,
        new_state=new_state,
        severity=severity,
        source_as_of=source_as_of,
        evidence_refs=evidence_refs,
        user_visible=user_visible,
        dedupe_key=_digest(dedupe_input),
    )


def detect_changes(
    previous: CompanyMonitoringSnapshot,
    current: CompanyMonitoringSnapshot,
) -> tuple[DetectedChange, ...]:
    """Return deterministic changes by stable semantic coordinate."""

    if previous.company_id != current.company_id:
        raise ValueError("monitoring snapshots must belong to the same company")
    old_facts = {_coordinate(item): item for item in previous.facts}
    new_facts = {_coordinate(item): item for item in current.facts}
    old_business = {
        _coordinate(item): item for item in previous.last_known_business_facts
    }
    privacy_blocked = set(previous.privacy_blocked_coordinates) | set(
        current.privacy_blocked_coordinates
    )
    changes = []
    for coordinate in sorted(set(old_facts) | set(new_facts)):
        if _privacy_coordinate_digest(current.company_id, coordinate) in privacy_blocked:
            continue
        old_observed = old_facts.get(coordinate)
        new_observed = new_facts.get(coordinate)
        if new_observed is not None and new_observed["state"] in _BUSINESS_STATES:
            # A coverage gap cannot erase the last trusted business value.
            # Recovery to that value is not a new business fact.
            change = _change(
                current.company_id,
                old_business.get(coordinate),
                new_observed,
            )
        else:
            change = _change(current.company_id, old_observed, new_observed) if (
                old_observed is not None or new_observed is not None
            ) else None
        if change is not None:
            changes.append(change)
    return tuple(changes)


def persist_events(
    session: Session,
    changes: Iterable[DetectedChange],
    *,
    detected_at: datetime | None = None,
) -> tuple[MonitoringEvent, ...]:
    """Idempotently persist canonical events and return existing conflicts."""

    when = _now(detected_at)
    events: list[MonitoringEvent] = []
    for change in changes:
        event_id = uuid4()
        event_ref = f"event:{uuid5(_EVENT_NAMESPACE, change.dedupe_key)}"
        inserted = session.scalar(
            insert(MonitoringEvent)
            .values(
                id=event_id,
                event_ref=event_ref,
                company_id=change.company_id,
                fact_ref=change.fact_ref,
                origin=change.origin,
                event_type=change.event_type,
                change_kind=change.change_kind,
                section_key=change.section_key,
                field_key=change.field_key,
                period_identity=change.period_identity,
                item_identity=change.item_identity,
                source_code=change.source_code,
                old_value=change.old_value,
                new_value=change.new_value,
                old_state=change.old_state,
                new_state=change.new_state,
                severity=change.severity,
                severity_policy_version=SEVERITY_POLICY_VERSION,
                detected_at=when,
                source_as_of=change.source_as_of,
                evidence_refs=list(change.evidence_refs),
                dedupe_key=change.dedupe_key,
                user_visible=change.user_visible,
            )
            .on_conflict_do_nothing(index_elements=[MonitoringEvent.dedupe_key])
            .returning(MonitoringEvent.id)
        )
        event_row = session.get(MonitoringEvent, inserted) if inserted else session.scalar(
            sa.select(MonitoringEvent).where(MonitoringEvent.dedupe_key == change.dedupe_key)
        )
        if event_row is None:
            raise RuntimeError("monitoring event upsert did not resolve a row")
        events.append(event_row)
    session.flush()
    return tuple(events)


def _audit(
    session: Session,
    *,
    workspace_id: UUID,
    user_id: UUID,
    action: str,
    target_type: str,
    target_ref: str,
    outcome: str,
) -> None:
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=user_id,
            action=action,
            target_type=target_type,
            target_ref=target_ref,
            outcome=outcome,
        )
    )


def _saved_company(
    session: Session,
    workspace_id: UUID,
    company_id: int,
    *,
    lock: bool = False,
) -> SavedCompany | None:
    query = sa.select(SavedCompany).where(
        SavedCompany.workspace_id == workspace_id,
        SavedCompany.company_id == company_id,
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def subscribe_company(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
    now: datetime | None = None,
) -> tuple[MonitoringSubscription, bool]:
    """Enable monitoring and capture a zero-event baseline atomically."""

    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
        lock_entitlement=True,
    )
    company = resolve_legal_company(session, inn)
    if _saved_company(session, workspace_id, company.id, lock=True) is None:
        raise ActionDenied(
            "saved_company_required",
            "Сначала сохраните компанию в этом Workspace.",
            status_code=409,
        )
    existing = session.scalar(
        sa.select(MonitoringSubscription)
        .where(
            MonitoringSubscription.workspace_id == workspace_id,
            MonitoringSubscription.company_id == company.id,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.status == "PAUSED":
            raise ActionDenied(
                "subscription_paused",
                "Подписка приостановлена; используйте возобновление.",
                status_code=409,
            )
        _audit(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            action="monitoring.subscribe",
            target_type="company",
            target_ref=company.inn,
            outcome="already_active",
        )
        session.flush()
        return existing, False
    when = _now(now)
    baseline = capture_snapshot(session, company_id=company.id, captured_at=when)
    subscription = MonitoringSubscription(
        id=uuid4(),
        workspace_id=workspace_id,
        company_id=company.id,
        created_by_user_id=user_id,
        status="ACTIVE",
        started_at=when,
        last_checked_at=when,
        baseline_snapshot_id=baseline.id,
    )
    session.add(subscription)
    _audit(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action="monitoring.subscribe",
        target_type="company",
        target_ref=company.inn,
        outcome="success",
    )
    session.flush()
    return subscription, True


def _subscription_for_write(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
) -> tuple[Company, MonitoringSubscription]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
        lock_entitlement=True,
    )
    company = resolve_legal_company(session, inn)
    subscription = session.scalar(
        sa.select(MonitoringSubscription)
        .where(
            MonitoringSubscription.workspace_id == workspace_id,
            MonitoringSubscription.company_id == company.id,
        )
        .with_for_update()
    )
    if subscription is None:
        raise ActionDenied(
            "monitoring_not_active",
            "Мониторинг компании не активирован.",
            status_code=409,
        )
    return company, subscription


def pause_subscription(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
    now: datetime | None = None,
) -> tuple[MonitoringSubscription, bool]:
    company, subscription = _subscription_for_write(
        session, user_id=user_id, workspace_id=workspace_id, inn=inn
    )
    changed = subscription.status != "PAUSED"
    if changed:
        subscription.status = "PAUSED"
        subscription.paused_at = _now(now)
    _audit(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action="monitoring.pause",
        target_type="company",
        target_ref=company.inn,
        outcome="success" if changed else "already_paused",
    )
    session.flush()
    return subscription, changed


def resume_subscription(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
    now: datetime | None = None,
) -> tuple[MonitoringSubscription, bool]:
    company, subscription = _subscription_for_write(
        session, user_id=user_id, workspace_id=workspace_id, inn=inn
    )
    changed = subscription.status != "ACTIVE"
    if changed:
        if _saved_company(session, workspace_id, company.id, lock=True) is None:
            raise ActionDenied(
                "saved_company_required",
                "Сначала сохраните компанию в этом Workspace.",
                status_code=409,
            )
        when = _now(now)
        baseline = capture_snapshot(session, company_id=company.id, captured_at=when)
        subscription.status = "ACTIVE"
        subscription.paused_at = None
        subscription.last_checked_at = when
        subscription.baseline_snapshot_id = baseline.id
    _audit(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action="monitoring.resume",
        target_type="company",
        target_ref=company.inn,
        outcome="success" if changed else "already_active",
    )
    session.flush()
    return subscription, changed


def get_monitoring_state(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
) -> MonitoringState:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    company = resolve_legal_company(session, inn)
    if _saved_company(session, workspace_id, company.id) is None:
        return MonitoringState(
            state="NOT_ACTIVE",
            message="Сначала сохраните компанию в этом Workspace.",
            can_manage=False,
            denial_code="saved_company_required",
        )
    can_manage, denial, _limit = action_state(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
    )
    subscription = session.scalar(
        sa.select(MonitoringSubscription).where(
            MonitoringSubscription.workspace_id == workspace_id,
            MonitoringSubscription.company_id == company.id,
        )
    )
    if denial == "entitlement_blocked":
        return MonitoringState(
            state="NOT_ACTIVE",
            message="Мониторинг не подключён для этого Workspace.",
            can_manage=False,
            denial_code=denial,
        )
    if subscription is None:
        message = (
            "Мониторинг не подключён для этого Workspace."
            if denial != "permission_denied"
            else "У вашей роли нет права управлять мониторингом."
        )
        return MonitoringState(
            state="NOT_ACTIVE",
            message=message,
            can_manage=can_manage,
            denial_code=denial,
        )
    return MonitoringState(
        state=subscription.status,
        message=(
            "Мониторинг активен; изменения появляются после контролируемого сканирования."
            if subscription.status == "ACTIVE"
            else "Мониторинг приостановлен; новые события в ленту не доставляются."
        ),
        can_manage=can_manage,
        denial_code=denial,
        subscription_id=subscription.id,
        started_at=subscription.started_at,
        paused_at=subscription.paused_at,
        last_checked_at=subscription.last_checked_at,
    )


def _entitlement_enabled(session: Session, workspace_id: UUID) -> bool:
    return bool(
        session.scalar(
            sa.select(WorkspaceEntitlement.enabled).where(
                WorkspaceEntitlement.workspace_id == workspace_id,
                WorkspaceEntitlement.entitlement_key == "monitoring.enabled",
            )
        )
    )


def fanout_events(
    session: Session,
    *,
    subscription: MonitoringSubscription,
    events: Iterable[MonitoringEvent],
) -> int:
    """Idempotently deliver visible events to one active entitled subscription."""

    if subscription.status != "ACTIVE" or not _entitlement_enabled(
        session, subscription.workspace_id
    ):
        return 0
    created = 0
    for event_row in events:
        if event_row.company_id != subscription.company_id:
            raise ValueError("monitoring event company does not match subscription")
        if not event_row.user_visible:
            continue
        inserted = session.scalar(
            insert(WorkspaceFeedEntry)
            .values(
                id=uuid4(),
                workspace_id=subscription.workspace_id,
                subscription_id=subscription.id,
                event_id=event_row.id,
            )
            .on_conflict_do_nothing(
                index_elements=[WorkspaceFeedEntry.subscription_id, WorkspaceFeedEntry.event_id]
            )
            .returning(WorkspaceFeedEntry.id)
        )
        created += int(inserted is not None)
    session.flush()
    return created


def monitor_company_once(
    session: Session,
    *,
    company_id: int,
    detected_at: datetime | None = None,
) -> MonitoringRunResult:
    """Run one deterministic system scan; production scheduling is out of scope."""

    subscriptions = tuple(
        session.scalars(
            sa.select(MonitoringSubscription)
            .join(
                WorkspaceEntitlement,
                sa.and_(
                    WorkspaceEntitlement.workspace_id == MonitoringSubscription.workspace_id,
                    WorkspaceEntitlement.entitlement_key == "monitoring.enabled",
                    WorkspaceEntitlement.enabled.is_(True),
                ),
            )
            .where(
                MonitoringSubscription.company_id == company_id,
                MonitoringSubscription.status == "ACTIVE",
            )
            .order_by(MonitoringSubscription.id)
            .with_for_update()
        ).all()
    )
    if not subscriptions:
        return MonitoringRunResult(company_id, None, 0, 0, 0, 0)
    when = _now(detected_at)
    facts, restricted, risk_ref, summary_ref = _observed_semantic_state(
        session, company_id=company_id
    )
    all_dedupe_keys: set[str] = set()
    all_event_ids: set[UUID] = set()
    feed_count = 0
    first_snapshot_id: UUID | None = None
    for subscription in subscriptions:
        previous = (
            session.get(CompanyMonitoringSnapshot, subscription.baseline_snapshot_id)
            if subscription.baseline_snapshot_id
            else None
        )
        current = _create_snapshot(
            session,
            company_id=company_id,
            facts=facts,
            restricted_coordinates=restricted,
            risk_ref=risk_ref,
            summary_ref=summary_ref,
            previous_snapshot=previous,
            captured_at=when,
        )
        if first_snapshot_id is None:
            first_snapshot_id = current.id
        changes = detect_changes(previous, current) if previous is not None else ()
        events = persist_events(session, changes, detected_at=when)
        all_dedupe_keys.update(change.dedupe_key for change in changes)
        all_event_ids.update(event_row.id for event_row in events)
        feed_count += fanout_events(session, subscription=subscription, events=events)
        subscription.baseline_snapshot_id = current.id
        subscription.last_checked_at = when
    session.flush()
    return MonitoringRunResult(
        company_id=company_id,
        snapshot_id=first_snapshot_id,
        subscription_count=len(subscriptions),
        detected_change_count=len(all_dedupe_keys),
        canonical_event_count=len(all_event_ids),
        feed_entry_count=feed_count,
    )


def list_workspace_feed(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    limit: int = 100,
) -> tuple[WorkspaceFeedItem, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
    )
    bounded_limit = max(1, min(int(limit), 200))
    rows = session.execute(
        sa.select(WorkspaceFeedEntry, MonitoringEvent, Company)
        .join(MonitoringEvent, MonitoringEvent.id == WorkspaceFeedEntry.event_id)
        .join(Company, Company.id == MonitoringEvent.company_id)
        .where(WorkspaceFeedEntry.workspace_id == workspace_id)
        .order_by(
            MonitoringEvent.detected_at.desc(),
            WorkspaceFeedEntry.created_at.desc(),
            WorkspaceFeedEntry.id.desc(),
        )
        .limit(bounded_limit)
    ).all()
    return tuple(
        WorkspaceFeedItem(
            entry_id=entry.id,
            subscription_id=entry.subscription_id,
            event_ref=event_row.event_ref,
            company_id=company.id,
            company_name=company.short_name or company.name,
            inn=company.inn,
            title=EVENT_TITLES.get(event_row.event_type, EVENT_TITLES["GENERIC_FACT_CHANGED"]),
            event_type=event_row.event_type,
            change_kind=event_row.change_kind,
            old_value=event_row.old_value,
            new_value=event_row.new_value,
            old_state=event_row.old_state,
            new_state=event_row.new_state,
            severity=event_row.severity,
            detected_at=event_row.detected_at,
            source_code=event_row.source_code,
            evidence_refs=tuple(event_row.evidence_refs or ()),
            created_at=entry.created_at,
            read_at=entry.read_at,
        )
        for entry, event_row, company in rows
    )


def mark_feed_entry_read(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    entry_id: UUID,
    now: datetime | None = None,
) -> tuple[WorkspaceFeedEntry, bool]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
    )
    entry = session.scalar(
        sa.select(WorkspaceFeedEntry)
        .where(
            WorkspaceFeedEntry.id == entry_id,
            WorkspaceFeedEntry.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if entry is None:
        raise ActionDenied(
            "feed_entry_not_found",
            "Событие ленты не найдено.",
            status_code=404,
        )
    changed = entry.read_at is None
    if changed:
        entry.read_at = _now(now)
    _audit(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action="monitoring.feed.read",
        target_type="workspace_feed_entry",
        target_ref=str(entry.id),
        outcome="success" if changed else "already_read",
    )
    session.flush()
    return entry, changed
