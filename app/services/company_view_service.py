"""Semantic company fact normalization, selection, persistence and views."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from uuid import UUID, uuid5

import sqlalchemy as sa
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.company_view_v1 import (
    Audience,
    ContactScope,
    ContactType,
    CompanyResolutionV1,
    CompanyViewModelV1,
    CompanyViewSectionV1,
    DataState,
    EvidenceSourceClass,
    FactAnchor,
    FactRights,
    FinanceMetric,
    FinanceMetricCode,
    FinancePeriod,
    FinancePeriodKind,
    Freshness,
    IndividualEntrepreneurRegistration,
    PersonIdentifier,
    PersonIdentifierType,
    PublicContactValue,
    RelatedCompany,
    RelatedPersonRelation,
    RelatedPersonRelationType,
    RelatedPersonValue,
    RelationStatus,
    ResolutionState,
    SemanticEvidence,
    SemanticFact,
)
from app.models.company import Company
from app.models.semantic_fact import CompanySemanticFact


SECTION_KEYS = (
    "identity",
    "status",
    "registration",
    "address",
    "activity",
    "management",
    "founders",
    "contacts",
    "capital",
    "finances",
    "employees",
    "tax",
    "enforcement",
    "licenses",
    "courts",
    "bankruptcy",
    "procurement",
    "restrictions",
    "inspections",
    "connections",
    "events",
    "risk",
    "summary",
    "source_coverage",
    "freshness",
    "limitations",
    "anchors",
    "links",
)

_ANCHOR_NAMESPACE = UUID("53cf9885-4a85-4c67-a630-15ef3eef18d4")
_SOURCE_RANK = {
    EvidenceSourceClass.OFFICIAL_PRIMARY: 400,
    EvidenceSourceClass.OFFICIAL_API_OPEN_DATA: 300,
    EvidenceSourceClass.AUTHORIZED_BRIDGE: 200,
    EvidenceSourceClass.DERIVED: 100,
}
_RIGHTS_RANK = {
    FactRights.PUBLIC: 1,
    FactRights.AUTHENTICATED_ONLY: 2,
    FactRights.INTERNAL_ONLY: 3,
}
_SECTION_STATE_RANK = {
    DataState.CONFLICTING_EVIDENCE: 100,
    DataState.PARSING_ERROR: 95,
    DataState.TIMEOUT: 90,
    DataState.SOURCE_UNAVAILABLE: 85,
    DataState.STALE_DATA: 80,
    DataState.PARTIAL: 75,
    DataState.UNKNOWN: 70,
    DataState.NOT_CHECKED: 65,
    DataState.NOT_APPLICABLE: 20,
    DataState.FOUND: 10,
    DataState.NOT_FOUND: 10,
}


class SemanticFieldPolicy(BaseModel):
    """Field-level source authority and fail-closed public semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    official_sources: tuple[str, ...] = ()
    bridge_sources: tuple[str, ...] = ()
    derived: bool = False
    not_applicable_allowed: bool = False
    conflict_state: DataState = DataState.CONFLICTING_EVIDENCE
    stale_state: DataState = DataState.STALE_DATA
    unknown_state: DataState = DataState.UNKNOWN
    source_unavailable_state: DataState = DataState.SOURCE_UNAVAILABLE


def _policy(
    *official_sources: str,
    bridge: bool = False,
    derived: bool = False,
    not_applicable: bool = False,
) -> SemanticFieldPolicy:
    return SemanticFieldPolicy(
        official_sources=tuple(official_sources),
        bridge_sources=("FIRMOTEKA_AUTHORIZED_BRIDGE",) if bridge else (),
        derived=derived,
        not_applicable_allowed=not_applicable,
    )


# Exact field entries override section wildcards.  This registry is deliberately
# provider-agnostic at the contract boundary: it only defines which accepted
# evidence authorities may win a semantic coordinate.
SEMANTIC_FIELD_POLICIES: dict[str, SemanticFieldPolicy] = {
    "identity.*": _policy("MASTER_REGISTRY", bridge=True),
    "status.*": _policy("MASTER_REGISTRY", bridge=True),
    "registration.*": _policy("MASTER_REGISTRY", bridge=True),
    "address.*": _policy("MASTER_REGISTRY", bridge=True),
    "activity.*": _policy("MASTER_REGISTRY", bridge=True),
    "management.*": _policy("MASTER_REGISTRY", bridge=True),
    "founders.*": _policy("MASTER_REGISTRY", bridge=True),
    "contacts.*": _policy(bridge=True),
    "capital.*": _policy("MASTER_REGISTRY", bridge=True),
    "finances.REVENUE": _policy("REVEXP", bridge=True),
    "finances.EXPENSES": _policy("REVEXP", bridge=True),
    "finances.PROFIT_LOSS": _policy("REVEXP", bridge=True),
    "finances.NET_PROFIT": _policy("GIRBO", bridge=True),
    "finances.EQUITY": _policy("GIRBO", bridge=True),
    "finances.COMPANY_VALUE": _policy(bridge=True, derived=True),
    "employees.EMPLOYEE_COUNT": _policy("HEADCOUNT", bridge=True),
    "tax.paid": _policy("PAYTAX", bridge=True),
    "tax.debt": _policy("DEBTAM", bridge=True),
    "tax.offence": _policy("TAXOFFENCE"),
    "enforcement.*": _policy("FSSP", bridge=True),
    "licenses.*": _policy("ROSZDRAV_LICENSES", bridge=True),
    "courts.*": _policy("MOSCOW_COURTS_OFFICIAL"),
    "bankruptcy.*": _policy("FEDRESURS"),
    "procurement.*": _policy("EIS_RNP", not_applicable=True),
    "restrictions.*": _policy("CBR_WARNING_LIST"),
    "inspections.*": _policy("ERKNM", not_applicable=True),
    "connections.*": _policy("MASTER_REGISTRY", bridge=True, derived=True),
    "events.*": _policy(),
    "risk.*": _policy(derived=True),
    "summary.*": _policy(derived=True),
    "source_coverage.*": _policy(derived=True),
    "freshness.*": _policy(derived=True),
}


def semantic_field_policy(section_key: str, field_key: str) -> SemanticFieldPolicy:
    return SEMANTIC_FIELD_POLICIES.get(
        f"{section_key}.{field_key}",
        SEMANTIC_FIELD_POLICIES.get(f"{section_key}.*", SemanticFieldPolicy()),
    )


class SemanticCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    company_id: int = Field(gt=0)
    section_key: str
    field_key: str
    value: Any
    source_code: str
    source_class: EvidenceSourceClass
    evidence_identity: str
    source_ref: str | None = None
    source_data_date: date | None = None
    retrieved_at: datetime
    confidence: float = Field(ge=0, le=1)
    freshness: Freshness
    rights: FactRights
    state: DataState = DataState.FOUND
    period_identity: str = ""
    item_identity: str = ""
    limitations: tuple[str, ...] = ()


def _aware(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback or datetime.now(UTC)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            pass
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _comparison_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _comparison_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_comparison_value(child) for child in value]
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
        return format(Decimal(value.strip()).normalize(), "f")
    return _json_value(value)


def _materially_equal(left: Any, right: Any) -> bool:
    return _canonical(_comparison_value(left)) == _canonical(_comparison_value(right))


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    normalized = re.sub(r"[^0-9,.-]", "", str(value).replace("\xa0", " "))
    if not normalized:
        return None
    if "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _legal_form(value: Any) -> dict[str, str | None] | None:
    if isinstance(value, dict):
        code = str(value.get("code")).strip() if value.get("code") is not None else None
        name = str(value.get("name") or value.get("title") or "").strip() or None
        return {"code": code, "name": name} if code or name else None
    name = str(value or "").strip()
    return {"code": None, "name": name} if name else None


def _coordinate(candidate: SemanticCandidate) -> str:
    return "|".join(
        (
            str(candidate.company_id),
            candidate.section_key,
            candidate.field_key,
            candidate.period_identity,
            candidate.item_identity,
        )
    )


def _anchor(candidate: SemanticCandidate) -> FactAnchor:
    coordinate = _coordinate(candidate)
    item_coordinate = "|".join(
        (
            str(candidate.company_id),
            candidate.section_key,
            candidate.period_identity,
            candidate.item_identity or candidate.field_key,
        )
    )
    return FactAnchor(
        fact_ref=f"fact:{uuid5(_ANCHOR_NAMESPACE, 'fact|' + coordinate)}",
        item_ref=f"item:{uuid5(_ANCHOR_NAMESPACE, 'item|' + item_coordinate)}",
        company_id=candidate.company_id,
        section_key=candidate.section_key,
        field_key=candidate.field_key,
        period_identity=candidate.period_identity,
        item_identity=candidate.item_identity,
    )


def _evidence(candidate: SemanticCandidate) -> SemanticEvidence:
    identity = "|".join((_coordinate(candidate), candidate.source_code, candidate.evidence_identity))
    return SemanticEvidence(
        evidence_ref=f"evidence:{uuid5(_ANCHOR_NAMESPACE, identity)}",
        source_code=candidate.source_code,
        source_class=candidate.source_class,
        source_ref=candidate.source_ref,
        value=_json_value(candidate.value),
        source_data_date=candidate.source_data_date,
        retrieved_at=candidate.retrieved_at,
        confidence=candidate.confidence,
        freshness=candidate.freshness,
        rights=candidate.rights,
        limitations=candidate.limitations,
    )


def _selection_key(candidate: SemanticCandidate) -> tuple[Any, ...]:
    source_date = candidate.source_data_date.toordinal() if candidate.source_data_date else -1
    retrieved = candidate.retrieved_at.timestamp()
    policy = semantic_field_policy(candidate.section_key, candidate.field_key)
    if candidate.source_code in policy.official_sources:
        authority_rank = 1_000
    elif candidate.source_code in policy.bridge_sources:
        authority_rank = 500
    else:
        authority_rank = _SOURCE_RANK[candidate.source_class]
    return (
        -authority_rank,
        -source_date,
        -retrieved,
        candidate.source_code,
        candidate.evidence_identity,
    )


def select_semantic_facts(
    candidates: Iterable[SemanticCandidate], *, observed_at: datetime | None = None
) -> tuple[SemanticFact, ...]:
    """Select candidates without erasing alternatives or historic disagreement."""

    now = _aware(observed_at)
    grouped: dict[str, list[SemanticCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[_coordinate(candidate)].append(candidate)
    facts: list[SemanticFact] = []
    for coordinate in sorted(grouped):
        ordered = sorted(grouped[coordinate], key=_selection_key)
        selected = ordered[0]
        selected_evidence = _evidence(selected)
        alternatives = tuple(_evidence(item) for item in ordered[1:])
        comparable = tuple(
            item
            for item in ordered[1:]
            if item.state in {DataState.FOUND, DataState.STALE_DATA}
        )
        conflict = (
            selected.state in {DataState.FOUND, DataState.STALE_DATA}
            and any(not _materially_equal(item.value, selected.value) for item in comparable)
        )
        state = (
            DataState.CONFLICTING_EVIDENCE
            if conflict
            else DataState.STALE_DATA
            if selected.state == DataState.FOUND and selected.freshness == Freshness.STALE
            else selected.state
        )
        facts.append(
            SemanticFact(
                anchor=_anchor(selected),
                selected_evidence=selected_evidence,
                alternative_evidence=alternatives,
                state=state,
                rights=selected.rights,
                created_at=now,
                updated_at=now,
            )
        )
    return tuple(facts)


def _candidate(
    company_id: int,
    section_key: str,
    field_key: str,
    value: Any,
    *,
    source_code: str,
    source_class: EvidenceSourceClass,
    evidence_identity: str,
    retrieved_at: datetime,
    source_ref: str | None = None,
    source_data_date: date | None = None,
    rights: FactRights,
    period_identity: str = "",
    item_identity: str = "",
    confidence: float = 1.0,
    freshness: Freshness | None = None,
    state: DataState = DataState.FOUND,
    limitations: tuple[str, ...] = (),
) -> SemanticCandidate | None:
    if state == DataState.FOUND and value in (None, "", [], {}):
        return None
    return SemanticCandidate(
        company_id=company_id,
        section_key=section_key,
        field_key=field_key,
        value=_json_value(value),
        source_code=source_code,
        source_class=source_class,
        evidence_identity=evidence_identity,
        source_ref=source_ref,
        source_data_date=source_data_date,
        retrieved_at=retrieved_at,
        confidence=confidence,
        freshness=freshness or (Freshness.CURRENT if source_data_date else Freshness.UNKNOWN),
        rights=rights,
        state=state,
        period_identity=period_identity,
        item_identity=item_identity,
        limitations=limitations,
    )


def _append(values: list[SemanticCandidate], candidate: SemanticCandidate | None) -> None:
    if candidate is not None:
        values.append(candidate)


def _event_description(item: dict[str, Any]) -> str | None:
    parts = item.get("parts") or ()
    if not isinstance(parts, list):
        return None
    text = "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict)
    )
    return re.sub(r"\s+", " ", text).strip() or None


def _event_identity(item: dict[str, Any], description: str | None) -> str:
    """Return a semantic event coordinate without provider row IDs or URLs."""

    event_date = _date(item.get("date"))
    kind = str(item.get("kind") or "event").strip().casefold()
    folded = (description or "").casefold()
    discriminator = next(
        (
            code
            for marker, code in (
                ("пенсион", "pension-fund"),
                ("социальн", "social-insurance"),
                ("налогов", "tax-authority"),
                ("учред", "founders"),
                ("зарегистрирована", "registration"),
                ("микропредприят", "sme-register"),
            )
            if marker in folded
        ),
        "event",
    )
    return f"{event_date.isoformat() if event_date else 'undated'}:{kind}:{discriminator}"


def _semantic_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    return re.sub(r"\s+", " ", str(value)).strip() or None


def _safe_amount_breakdown(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, dict):
            continue
        name = _semantic_text(item.get("name"))
        amount = _decimal(item.get("amount"))
        if name and amount is not None:
            result.append({"name": name, "amount": str(amount)})
    return result


def _safe_collection_value(collection_key: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Whitelist public business fields; never forward provider objects verbatim."""

    if collection_key == "events":
        description = _event_description(item)
        event_date = _date(item.get("date"))
        if not (description or event_date):
            return None
        return {
            "date": event_date,
            "description": description,
        }
    if collection_key == "licenses":
        value = {
            "number": _semantic_text(item.get("number") or item.get("license_number")),
            "status": _semantic_text(item.get("status")),
            "authority": _semantic_text(item.get("authority") or item.get("authority_name")),
            "activity": _semantic_text(item.get("activity") or item.get("activity_type")),
            "start_date": _date(item.get("start_date") or item.get("date")),
            "end_date": _date(item.get("end_date")),
        }
    elif collection_key == "divisions":
        value = {
            "name": _semantic_text(item.get("name") or item.get("title")),
            "type": _semantic_text(item.get("type")),
            "address": _semantic_text(item.get("address")),
        }
    else:
        return None
    return {key: child for key, child in value.items() if child not in (None, "", [], {})} or None


def _safe_enforcement_case(item: dict[str, Any]) -> dict[str, Any] | None:
    number = str(item.get("number") or "").strip()
    if not number:
        return None
    value = {
        "number": number,
        "started_on": _date(item.get("date")),
        "document_type": _semantic_text(item.get("doc_type")),
        "document_date": _date(item.get("doc_date")),
        "document_number": _semantic_text(item.get("doc_number")),
        "subject": _semantic_text(item.get("subject")),
        "state": _semantic_text(item.get("state") or item.get("status")),
        "amount_due": _decimal(item.get("amount_due")),
        "amount_remaining": _decimal(item.get("amount_rest")),
        "department": _semantic_text(item.get("department")),
    }
    return {key: child for key, child in value.items() if child not in (None, "", [], {})}


def _person_name_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _related_person_ref(company_id: int, name: str) -> str:
    identity = f"related-person|{company_id}|{_person_name_key(name)}"
    return f"person:{uuid5(_ANCHOR_NAMESPACE, identity)}"


def _historical_status(value: dict[str, Any]) -> RelationStatus:
    if any(
        _date(value.get(key)) is not None
        for key in ("until", "end_date", "termination_date", "left_on")
    ):
        return RelationStatus.HISTORICAL
    status = str(value.get("status") or value.get("relation_status") or "").casefold()
    if any(
        marker in status
        for marker in (
            "historical",
            "former",
            "terminated",
            "inactive",
            "бывш",
            "прекрат",
            "исключ",
        )
    ):
        return RelationStatus.HISTORICAL
    return RelationStatus.CURRENT


def _related_company(projection: dict[str, Any]) -> RelatedCompany:
    inn = str(projection.get("requested_inn") or projection.get("rendered_inn") or "")
    name = _semantic_text(projection.get("name") or projection.get("full_name"))
    return RelatedCompany(inn=inn, name=name or f"ИНН {inn}")


def _related_person_values(
    projection: dict[str, Any], *, company_id: int
) -> dict[str, RelatedPersonValue]:
    people: dict[str, dict[str, Any]] = {}

    def add_relation(
        raw: dict[str, Any],
        relation_type: RelatedPersonRelationType,
        *,
        role: Any = None,
        context: Any = None,
        share: Any = None,
        since: Any = None,
        until: Any = None,
    ) -> None:
        name = _semantic_text(raw.get("name"))
        if not name:
            return
        key = _person_name_key(name)
        entry = people.setdefault(
            key,
            {
                "name": name,
                "identifiers": set(),
                "relations": [],
                "ip": None,
            },
        )
        inn = _semantic_text(raw.get("tin") or raw.get("inn"))
        if inn and re.fullmatch(r"\d{12}", inn):
            entry["identifiers"].add((PersonIdentifierType.INN, inn))
        ogrnip = _semantic_text(raw.get("ogrnip") or raw.get("psrn"))
        if ogrnip and re.fullmatch(r"\d{15}", ogrnip):
            entry["identifiers"].add((PersonIdentifierType.OGRNIP, ogrnip))
        end_date = _date(
            until
            or raw.get("until")
            or raw.get("end_date")
            or raw.get("termination_date")
            or raw.get("left_on")
        )
        status = RelationStatus.HISTORICAL if end_date else _historical_status(raw)
        relation = RelatedPersonRelation(
            relation_type=relation_type,
            role=_semantic_text(role),
            context=_semantic_text(context),
            share=_semantic_text(share),
            since=_date(since or raw.get("since") or raw.get("date") or raw.get("registration_date")),
            until=end_date,
            status=status,
        )
        if relation not in entry["relations"]:
            entry["relations"].append(relation)
        if ogrnip and re.fullmatch(r"\d{15}", ogrnip):
            termination_date = _date(raw.get("termination_date"))
            ip_status = _historical_status(raw) if termination_date is None else RelationStatus.HISTORICAL
            entry["ip"] = IndividualEntrepreneurRegistration(
                ogrnip=ogrnip,
                status=_semantic_text(raw.get("ip_status") or raw.get("status")),
                registration_date=_date(raw.get("ip_registration_date") or raw.get("registration_date")),
                termination_date=termination_date,
                current_status=ip_status,
            )
            ip_relation = RelatedPersonRelation(
                relation_type=RelatedPersonRelationType.INDIVIDUAL_ENTREPRENEUR,
                role="Индивидуальный предприниматель",
                context="Публичная регистрация ИП",
                since=entry["ip"].registration_date,
                until=entry["ip"].termination_date,
                status=entry["ip"].current_status,
            )
            if ip_relation not in entry["relations"]:
                entry["relations"].append(ip_relation)

    manager_details = projection.get("manager_details")
    manager_raw = dict(manager_details) if isinstance(manager_details, dict) else {}
    manager_raw["name"] = manager_raw.get("name") or projection.get("manager")
    manager_raw["position"] = manager_raw.get("position") or projection.get("manager_position")
    if manager_raw.get("name"):
        add_relation(
            manager_raw,
            RelatedPersonRelationType.MANAGER,
            role=manager_raw.get("position"),
            context="Руководство компании",
            since=manager_raw.get("date"),
        )

    founder_groups = projection.get("founders") or ()
    if isinstance(founder_groups, dict):
        founder_groups = founder_groups.get("items") or (founder_groups,)
    for group in founder_groups if isinstance(founder_groups, list) else ():
        if not isinstance(group, dict):
            continue
        items = group.get("items")
        rows = items or ((group,) if group.get("name") else ())
        for founder in rows:
            if not isinstance(founder, dict):
                continue
            inn = _semantic_text(founder.get("tin") or founder.get("inn"))
            ogrnip = _semantic_text(founder.get("ogrnip") or founder.get("psrn"))
            group_type = str(founder.get("type") or group.get("type") or "").casefold()
            is_person = bool(
                (inn and re.fullmatch(r"\d{12}", inn))
                or (ogrnip and re.fullmatch(r"\d{15}", ogrnip))
                or any(marker in group_type for marker in ("person", "физ", "individual"))
            )
            if not is_person:
                continue
            explicit_relation = founder.get("relation_type") or group.get("relation_type")
            relation_marker = str(
                explicit_relation or group.get("title") or "founder"
            ).casefold()
            if any(marker in relation_marker for marker in ("participant", "участник")):
                relation_type = RelatedPersonRelationType.PARTICIPANT
            elif not explicit_relation or any(
                marker in relation_marker for marker in ("founder", "учред")
            ):
                relation_type = RelatedPersonRelationType.FOUNDER
            elif any(marker in relation_marker for marker in ("entrepreneur", "ип")):
                relation_type = RelatedPersonRelationType.INDIVIDUAL_ENTREPRENEUR
            else:
                relation_type = RelatedPersonRelationType.OTHER_PUBLIC_RELATION
            add_relation(
                founder,
                relation_type,
                role=founder.get("role"),
                context=group.get("title") or "Учредители и участники компании",
                share=founder.get("share"),
                since=founder.get("date"),
            )

    related_company = _related_company(projection)
    result: dict[str, RelatedPersonValue] = {}
    for key, entry in people.items():
        relations = tuple(
            sorted(
                entry["relations"],
                key=lambda item: (
                    item.relation_type.value,
                    item.role or "",
                    item.share or "",
                    item.since or date.min,
                ),
            )
        )
        identifiers = tuple(
            PersonIdentifier(identifier_type=identifier_type, value=value)
            for identifier_type, value in sorted(
                entry["identifiers"], key=lambda item: (item[0].value, item[1])
            )
        )
        relation_types = tuple(dict.fromkeys(item.relation_type for item in relations))
        current_status = (
            RelationStatus.CURRENT
            if any(item.status == RelationStatus.CURRENT for item in relations)
            else RelationStatus.HISTORICAL
        )
        manager = next(
            (item for item in relations if item.relation_type == RelatedPersonRelationType.MANAGER),
            None,
        )
        ownership = next(
            (
                item
                for item in relations
                if item.relation_type
                in {RelatedPersonRelationType.FOUNDER, RelatedPersonRelationType.PARTICIPANT}
            ),
            None,
        )
        result[key] = RelatedPersonValue(
            person_ref=_related_person_ref(company_id, entry["name"]),
            name=entry["name"],
            relation_types=relation_types,
            relations=relations,
            identifiers=identifiers,
            related_company=related_company,
            current_status=current_status,
            individual_entrepreneur=entry["ip"],
            type="person",
            position=manager.role if manager else None,
            share=ownership.share if ownership else None,
            since=ownership.since if ownership else manager.since if manager else None,
        )
    return result


def _contact_candidates(
    projection: dict[str, Any],
    *,
    company_id: int,
    people: dict[str, RelatedPersonValue],
    bridge: dict[str, Any],
) -> tuple[SemanticCandidate, ...]:
    contacts = projection.get("contacts")
    if not isinstance(contacts, dict):
        return ()
    accepted_keys = {
        "phone": ContactType.PHONE,
        "phones": ContactType.PHONE,
        "telephone": ContactType.PHONE,
        "telephones": ContactType.PHONE,
        "email": ContactType.EMAIL,
        "emails": ContactType.EMAIL,
        "e-mail": ContactType.EMAIL,
    }
    normalized: list[tuple[ContactType, PublicContactValue, date | None]] = []
    for raw_key, raw_values in contacts.items():
        contact_type = accepted_keys.get(str(raw_key).strip().casefold())
        if contact_type is None:
            continue
        values = raw_values if isinstance(raw_values, list) else (raw_values,)
        for raw in values:
            item = raw if isinstance(raw, dict) else {"value": raw}
            raw_value = item.get("value")
            if raw_value is None:
                raw_value = (
                    item.get("email") or item.get("email_address")
                    if contact_type == ContactType.EMAIL
                    else item.get("phone") or item.get("telephone")
                )
            value = _semantic_text(raw_value)
            if not value:
                continue
            if contact_type == ContactType.EMAIL:
                value = value.casefold()
                if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is None:
                    continue
            elif len(re.sub(r"\D", "", value)) < 7:
                continue
            scope_marker = str(item.get("scope") or item.get("contact_scope") or "").casefold()
            if scope_marker in {"corporate", "company", "organization", "корпоративный", "организация"}:
                scope = ContactScope.CORPORATE
            elif scope_marker in {"personal", "person", "личный", "персональный"}:
                scope = ContactScope.PERSONAL
            else:
                scope = ContactScope.UNKNOWN
            person_name = _semantic_text(
                item.get("person_name")
                or item.get("related_person_name")
                or item.get("owner_name")
            )
            person = people.get(_person_name_key(person_name)) if person_name else None
            normalized.append(
                (
                    contact_type,
                    PublicContactValue(
                        contact_type=contact_type,
                        contact_scope=scope,
                        value=value,
                        related_person_ref=person.person_ref if person else None,
                        person_name=person_name,
                        role_context=_semantic_text(item.get("role") or item.get("context")),
                        related_company=_related_company(projection),
                        current_status=_historical_status(item),
                    ),
                    _date(item.get("source_as_of") or item.get("source_data_date") or item.get("date")),
                )
            )
    normalized.sort(
        key=lambda item: (
            item[0].value,
            item[1].related_person_ref or "",
            item[1].contact_scope.value,
            item[1].value,
        )
    )
    slots: dict[tuple[str, str, str, str], int] = defaultdict(int)
    result: list[SemanticCandidate] = []
    for contact_type, contact, source_date in normalized:
        slot_key = (
            contact_type.value,
            contact.related_person_ref or "company",
            contact.contact_scope.value,
            contact.current_status.value,
        )
        slots[slot_key] += 1
        item_identity = ":".join((*slot_key, str(slots[slot_key])))
        _append(
            result,
            _candidate(
                company_id,
                "contacts",
                contact_type.value.casefold(),
                contact.model_dump(mode="json"),
                item_identity=item_identity,
                source_data_date=source_date,
                evidence_identity=f"{bridge['evidence_identity']}:contacts:{item_identity}",
                **{key: value for key, value in bridge.items() if key != "evidence_identity"},
            ),
        )
    return tuple(result)


def normalize_firmoteka_projection(
    projection: dict[str, Any],
    *,
    company_id: int,
    snapshot_identity: str,
    retrieved_at: datetime,
    include_contacts: bool = True,
) -> tuple[SemanticCandidate, ...]:
    """Map provider-shaped Firmoteka JSON to business coordinates."""

    if str(projection.get("rendered_inn") or projection.get("requested_inn")) != str(
        projection.get("requested_inn")
    ):
        return ()
    values: list[SemanticCandidate] = []
    bridge = dict(
        source_code="FIRMOTEKA_AUTHORIZED_BRIDGE",
        source_class=EvidenceSourceClass.AUTHORIZED_BRIDGE,
        evidence_identity=snapshot_identity,
        source_ref=_semantic_text(projection.get("url")),
        retrieved_at=retrieved_at,
        rights=FactRights.PUBLIC,
        confidence=0.75,
        limitations=("Сведения получены через авторизованный агрегатор, а не напрямую из первичного реестра.",),
    )
    egrul_date = _date(projection.get("fns_egrul_as_of"))
    scalar_map = (
        ("identity", "name", "name"),
        ("identity", "full_name", "full_name"),
        ("identity", "ogrn", "ogrn"),
        ("identity", "kpp", "kpp"),
        ("identity", "entity_type", "entity_type"),
        ("status", "status", "status_normalized"),
        ("registration", "registration_date", "registration_date"),
        ("registration", "termination_date", "termination_date"),
        ("address", "registered_address", "address"),
        ("address", "region", "region"),
        ("activity", "okved", "okved"),
        ("activity", "okved_name", "okved_name"),
    )
    for section, field, key in scalar_map:
        _append(
            values,
            _candidate(
                company_id,
                section,
                field,
                projection.get(key),
                source_data_date=egrul_date,
                **bridge,
            ),
        )
    _append(
        values,
        _candidate(
            company_id,
            "identity",
            "legal_form",
            _legal_form(projection.get("legal_form")),
            source_data_date=egrul_date,
            **bridge,
        ),
    )
    for item in projection.get("additional_okved") or ():
        text = str(item).strip()
        match = re.match(r"([0-9.]+)\s*(.*)", text)
        code = match.group(1) if match else text
        _append(
            values,
            _candidate(
                company_id,
                "activity",
                "additional_okved",
                {"code": code, "name": match.group(2).strip() or None if match else None},
                item_identity=code,
                source_data_date=egrul_date,
                **bridge,
            ),
        )
    people = _related_person_values(projection, company_id=company_id)
    manager = projection.get("manager")
    if manager:
        manager_identity = re.sub(r"\s+", " ", str(manager).casefold()).strip()
        manager_value = people.get(_person_name_key(manager))
        _append(
            values,
            _candidate(
                company_id,
                "management",
                "manager",
                (
                    manager_value.model_dump(mode="json")
                    if manager_value
                    else {"name": manager, "position": projection.get("manager_position")}
                ),
                item_identity=manager_identity,
                source_data_date=egrul_date,
                **bridge,
            ),
        )
    founder_groups = projection.get("founders") or ()
    if isinstance(founder_groups, dict):
        founder_groups = founder_groups.get("items") or (founder_groups,)
    for group in founder_groups if isinstance(founder_groups, list) else ():
        items = group.get("items") if isinstance(group, dict) else None
        for founder in items or ((group,) if isinstance(group, dict) and group.get("name") else ()):
            if not isinstance(founder, dict):
                continue
            identity = str(
                founder.get("tin")
                or founder.get("inn")
                or founder.get("ogrnip")
                or founder.get("ogrn")
                or founder.get("name")
                or ""
            ).strip()
            if not identity:
                continue
            person = people.get(_person_name_key(founder.get("name")))
            normalized = (
                person.model_dump(mode="json")
                if person
                else {
                    "name": founder.get("name"),
                    "type": founder.get("type") or (group.get("type") if isinstance(group, dict) else None),
                    "share": founder.get("share"),
                    "since": _date(founder.get("date")),
                }
            )
            _append(
                values,
                _candidate(
                    company_id,
                    "founders",
                    "founder",
                    normalized,
                    item_identity=identity,
                    source_data_date=egrul_date,
                    **bridge,
                ),
            )
    if include_contacts:
        values.extend(
            _contact_candidates(
                projection,
                company_id=company_id,
                people=people,
                bridge=bridge,
            )
        )
    capital = _decimal(projection.get("authorized_capital"))
    _append(
        values,
        _candidate(
            company_id,
            "capital",
            "authorized_capital",
            {"amount": str(capital), "currency": "RUB"} if capital is not None else None,
            source_data_date=egrul_date,
            **bridge,
        ),
    )

    metric_by_code = {"2110": FinanceMetricCode.REVENUE, "2400": FinanceMetricCode.NET_PROFIT, "1300": FinanceMetricCode.EQUITY}
    title_metrics = {
        "выручка": FinanceMetricCode.REVENUE,
        "расходы": FinanceMetricCode.EXPENSES,
        "чистая прибыль": FinanceMetricCode.NET_PROFIT,
        "итого капитал": FinanceMetricCode.EQUITY,
        "стоимость компании": FinanceMetricCode.COMPANY_VALUE,
    }
    for code, series in (projection.get("financials") or {}).items():
        if not isinstance(series, dict):
            continue
        title = str(series.get("title") or "").casefold()
        metric = metric_by_code.get(str(code))
        if metric is None:
            metric = next((item for marker, item in title_metrics.items() if marker in title), None)
        if metric is None:
            continue
        for point in series.get("values") or ():
            if not isinstance(point, dict) or not str(point.get("label", "")).isdigit():
                continue
            year = int(point["label"])
            amount = _decimal(point.get("value"))
            if amount is None:
                continue
            _append(
                values,
                _candidate(
                    company_id,
                    "finances",
                    metric.value,
                    {"value": str(amount), "currency": "RUB"},
                    period_identity=f"YEAR:{year}",
                    source_data_date=date(year, 12, 31),
                    **bridge,
                ),
            )
    for period, count in (projection.get("employee_counts") or {}).items():
        period_date = _date(period)
        if period_date is None or count is None:
            continue
        try:
            employee_count = int(count)
        except (TypeError, ValueError):
            continue
        period_identity = (
            f"YEAR:{period_date.year}"
            if period_date.month == 12 and period_date.day == 31
            else f"DATE:{period_date.isoformat()}"
        )
        _append(
            values,
            _candidate(
                company_id,
                "employees",
                FinanceMetricCode.EMPLOYEE_COUNT.value,
                employee_count,
                period_identity=period_identity,
                source_data_date=period_date,
                **bridge,
            ),
        )
    for debt in projection.get("tax_debts") or ():
        if not isinstance(debt, dict):
            continue
        data_date = _date(debt.get("date"))
        if data_date is None:
            continue
        total = _decimal(debt.get("total"))
        _append(
            values,
            _candidate(
                company_id,
                "tax",
                "debt",
                {
                    "total": str(total) if total is not None else None,
                    "currency": "RUB",
                    "breakdown": _safe_amount_breakdown(debt.get("breakdown")),
                },
                period_identity=f"DATE:{data_date.isoformat()}",
                source_data_date=data_date,
                **bridge,
            ),
        )
    for paid in projection.get("taxes_paid") or ():
        if not isinstance(paid, dict) or not str(paid.get("year", "")).isdigit():
            continue
        year = int(paid["year"])
        total = _decimal(paid.get("total"))
        _append(
            values,
            _candidate(
                company_id,
                "tax",
                "paid",
                {
                    "total": str(total) if total is not None else None,
                    "currency": "RUB",
                    "breakdown": _safe_amount_breakdown(paid.get("breakdown")),
                },
                period_identity=f"YEAR:{year}",
                source_data_date=date(year, 12, 31),
                **bridge,
            ),
        )
    for collection_key, section_key, field_key in (
        ("licenses", "licenses", "license"),
        ("divisions", "address", "division"),
        ("events", "events", "event"),
    ):
        for index, item in enumerate(projection.get(collection_key) or ()):
            if not isinstance(item, dict):
                continue
            normalized = _safe_collection_value(collection_key, item)
            if normalized is None:
                continue
            if collection_key == "events":
                identity = _event_identity(item, normalized.get("description"))
            elif collection_key == "licenses":
                identity = str(normalized.get("number") or f"license:{index}")
            else:
                identity = str(normalized.get("name") or normalized.get("address") or f"division:{index}")
            _append(
                values,
                _candidate(
                    company_id,
                    section_key,
                    field_key,
                    normalized,
                    item_identity=identity[:300],
                    source_data_date=_date(item.get("date") or item.get("start_date")) or egrul_date,
                    evidence_identity=f"{snapshot_identity}:{collection_key}:{index}",
                    **{key: value for key, value in bridge.items() if key != "evidence_identity"},
                ),
            )
    enforcement = projection.get("enforcements") or {}
    enforcement_date = _date(enforcement.get("snapshot")) if isinstance(enforcement, dict) else None
    if isinstance(enforcement, dict):
        _append(
            values,
            _candidate(
                company_id,
                "enforcement",
                "aggregate",
                {
                    "count": enforcement.get("count"),
                    "total_due": enforcement.get("total_due"),
                    "total_remaining": enforcement.get("total_rest"),
                    "completed_count": enforcement.get("completed_count"),
                    "closed_count": enforcement.get("closed_count"),
                },
                source_data_date=enforcement_date,
                **bridge,
            ),
        )
        for item in enforcement.get("items") or ():
            if not isinstance(item, dict):
                continue
            normalized = _safe_enforcement_case(item)
            if normalized is None:
                continue
            identity = str(normalized["number"])
            _append(
                values,
                _candidate(
                    company_id,
                    "enforcement",
                    "case",
                    normalized,
                    item_identity=identity,
                    source_data_date=enforcement_date,
                    evidence_identity=f"{snapshot_identity}:enforcement:{identity}",
                    **{key: value for key, value in bridge.items() if key != "evidence_identity"},
                ),
            )
    return tuple(values)


def _rows(cursor: Any, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def _one(cursor: Any, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor.execute(query, params)
    row = cursor.fetchone()
    return dict(row) if row else None


def load_semantic_candidates(cursor: Any, *, company_id: int) -> tuple[SemanticCandidate, ...]:
    company = _one(cursor, "SELECT * FROM companies WHERE id=%s", (company_id,))
    if company is None:
        return ()
    values: list[SemanticCandidate] = []
    updated_at = _aware(company.get("source_updated_at") or company.get("updated_at"))
    data_date = _date(company.get("master_data_date"))
    primary = dict(
        source_code="MASTER_REGISTRY",
        source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
        evidence_identity=f"master:{company.get('master_dataset_id') or company.get('source') or 'company'}",
        retrieved_at=updated_at,
        source_data_date=data_date,
        rights=FactRights.PUBLIC,
        confidence=1.0,
    )
    for section, field, key in (
        ("identity", "inn", "inn"),
        ("identity", "name", "name"),
        ("identity", "short_name", "short_name"),
        ("identity", "full_name", "full_name"),
        ("identity", "ogrn", "ogrn"),
        ("identity", "kpp", "kpp"),
        ("identity", "entity_type", "entity_type"),
        ("status", "status", "status"),
        ("registration", "registration_date", "registration_date"),
        ("registration", "termination_date", "termination_date"),
        ("address", "registered_address", "address"),
        ("address", "region_code", "region_code"),
        ("activity", "okved", "okved"),
        ("activity", "okved_name", "activity"),
    ):
        _append(values, _candidate(company_id, section, field, company.get(key), **primary))

    for manager in _rows(
        cursor,
        "SELECT * FROM company_managers WHERE company_id=%s AND is_current=TRUE ORDER BY id",
        (company_id,),
    ):
        name = manager.get("full_name") or " ".join(
            filter(None, (manager.get("last_name"), manager.get("first_name"), manager.get("middle_name")))
        )
        _append(
            values,
            _candidate(
                company_id,
                "management",
                "manager",
                {"name": name, "position": manager.get("position")},
                source_code="MASTER_REGISTRY",
                source_class=EvidenceSourceClass.OFFICIAL_PRIMARY,
                evidence_identity=f"manager:{manager.get('source_record_key') or name}",
                retrieved_at=_aware(manager.get("observed_at") or manager.get("created_at")),
                source_data_date=_date(manager.get("source_data_date") or data_date),
                rights=FactRights.PUBLIC,
                item_identity=re.sub(r"\s+", " ", str(name).casefold()).strip(),
                confidence=1.0,
            ),
        )
    for row in _rows(cursor, "SELECT * FROM company_revenue_expense_snapshots WHERE company_id=%s ORDER BY data_year, id", (company_id,)):
        year = int(row["data_year"])
        shared = dict(
            source_code="REVEXP",
            source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
            evidence_identity=f"revexp:{row.get('source_document_id')}:{year}",
            retrieved_at=_aware(row.get("updated_at") or row.get("created_at")),
            source_data_date=_date(row.get("data_date")),
            rights=FactRights.PUBLIC,
            period_identity=f"YEAR:{year}",
            confidence=1.0,
        )
        for metric, key in (
            (FinanceMetricCode.REVENUE, "revenue"),
            (FinanceMetricCode.EXPENSES, "expenses"),
            (FinanceMetricCode.PROFIT_LOSS, "profit_loss"),
        ):
            _append(values, _candidate(company_id, "finances", metric.value, {"value": str(row[key]), "currency": "RUB"}, **shared))
    for row in _rows(cursor, "SELECT * FROM company_headcounts WHERE company_id=%s ORDER BY year, id", (company_id,)):
        year = int(row["year"])
        _append(
            values,
            _candidate(
                company_id,
                "employees",
                FinanceMetricCode.EMPLOYEE_COUNT.value,
                int(row["employee_count"]),
                source_code="HEADCOUNT",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                evidence_identity=f"headcount:{row.get('source_document_id')}:{year}",
                retrieved_at=_aware(row.get("updated_at") or row.get("created_at")),
                source_data_date=_date(row.get("source_document_date")) or date(year, 12, 31),
                rights=FactRights.PUBLIC,
                period_identity=f"YEAR:{year}",
                confidence=1.0,
            ),
        )
    for row in _rows(cursor, "SELECT * FROM company_tax_payment_snapshots WHERE company_id=%s ORDER BY data_year, id", (company_id,)):
        year = int(row["data_year"])
        _append(
            values,
            _candidate(
                company_id,
                "tax",
                "paid",
                {
                    "total": str(row["total_amount"]),
                    "tax": str(row["tax_amount"]),
                    "insurance": str(row["insurance_amount"]),
                    "penalties": str(row["penalty_amount"]),
                    "currency": "RUB",
                },
                source_code="PAYTAX",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                evidence_identity=f"paytax:{row.get('source_document_id')}:{year}",
                retrieved_at=_aware(row.get("updated_at") or row.get("created_at")),
                source_data_date=_date(row.get("data_date")),
                rights=FactRights.PUBLIC,
                period_identity=f"YEAR:{year}",
                confidence=1.0,
            ),
        )
    for row in _rows(cursor, "SELECT * FROM company_tax_debt_snapshots WHERE company_id=%s ORDER BY data_date, id", (company_id,)):
        data_date = _date(row["data_date"])
        _append(
            values,
            _candidate(
                company_id,
                "tax",
                "debt",
                {
                    "total": str(row["total_debt"]),
                    "arrears": str(row["total_arrears"]),
                    "penalties": str(row["total_penalties"]),
                    "fines": str(row["total_fines"]),
                    "currency": "RUB",
                },
                source_code="DEBTAM",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                evidence_identity=f"debtam:{row.get('source_document_id')}:{data_date}",
                retrieved_at=_aware(row.get("updated_at") or row.get("created_at")),
                source_data_date=data_date,
                rights=FactRights.PUBLIC,
                period_identity=f"DATE:{data_date.isoformat()}",
                confidence=1.0,
            ),
        )
    for row in _rows(cursor, "SELECT * FROM company_tax_offences WHERE company_id=%s ORDER BY data_date, id", (company_id,)):
        data_date = _date(row["data_date"])
        _append(
            values,
            _candidate(
                company_id,
                "tax",
                "offence",
                {"fine_amount": str(row["fine_amount"]), "document_date": _date(row.get("document_date")), "currency": "RUB"},
                source_code="TAXOFFENCE",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                evidence_identity=f"taxoffence:{row.get('source_document_id')}",
                retrieved_at=_aware(row.get("updated_at") or row.get("created_at")),
                source_data_date=data_date,
                rights=FactRights.PUBLIC,
                item_identity=str(row.get("source_document_id")),
                confidence=1.0,
            ),
        )
    for row in _rows(cursor, "SELECT * FROM company_legal_events WHERE company_id=%s ORDER BY event_date, id", (company_id,)):
        identity = f"{row.get('source_code')}:{row.get('source_identifier')}:{row.get('event_type')}"
        _append(
            values,
            _candidate(
                company_id,
                "events",
                "legal_event",
                {"type": row.get("event_type"), "date": _date(row.get("event_date")), "status": row.get("status")},
                source_code=str(row.get("source_code") or "LEGAL_EVENT").upper(),
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                evidence_identity=identity,
                retrieved_at=_aware(row.get("retrieved_at") or row.get("checked_at")),
                source_data_date=_date(row.get("publication_date") or row.get("event_date")),
                rights=FactRights.PUBLIC,
                item_identity=identity,
                confidence=1.0,
            ),
        )
    for row in _rows(cursor, "SELECT * FROM roszdrav_license_entries WHERE inn=%s ORDER BY data_date, id", (company["inn"],)):
        identity = str(row.get("license_number"))
        _append(
            values,
            _candidate(
                company_id,
                "licenses",
                "license",
                {
                    "number": identity,
                    "authority": row.get("authority_name"),
                    "activity": row.get("activity_type"),
                    "start_date": _date(row.get("start_date")),
                    "end_date": _date(row.get("end_date")),
                },
                source_code="ROSZDRAV_LICENSES",
                source_class=EvidenceSourceClass.OFFICIAL_API_OPEN_DATA,
                evidence_identity=f"roszdrav:{row.get('record_key')}",
                retrieved_at=_aware(row.get("created_at")),
                source_data_date=_date(row.get("data_date")),
                rights=FactRights.PUBLIC,
                item_identity=identity,
                confidence=1.0,
            ),
        )
    snapshot = _one(
        cursor,
        "SELECT id, retrieved_at, source_as_of, projection FROM firmoteka_company_snapshots WHERE company_id=%s AND is_current=TRUE ORDER BY retrieved_at DESC LIMIT 1",
        (company_id,),
    )
    if snapshot:
        values.extend(
            normalize_firmoteka_projection(
                snapshot["projection"],
                company_id=company_id,
                snapshot_identity=f"snapshot:{snapshot['id']}",
                retrieved_at=_aware(snapshot["retrieved_at"]),
            )
        )
    return tuple(values)


def _risk_summary_refs(cursor: Any, company_id: int) -> tuple[str | None, str | None]:
    risk = _one(
        cursor,
        "SELECT assessment_id FROM company_risk_assessments_v3 WHERE company_id=%s ORDER BY calculated_at DESC, id DESC LIMIT 1",
        (company_id,),
    )
    if not risk:
        return None, None
    summary = _one(
        cursor,
        "SELECT summary_id, risk_assessment_id FROM company_summaries_v3 WHERE company_id=%s AND risk_assessment_id=%s ORDER BY generated_at DESC, id DESC LIMIT 1",
        (company_id, risk["assessment_id"]),
    )
    return str(risk["assessment_id"]), str(summary["summary_id"]) if summary else None


def _finance_metrics(facts: Iterable[SemanticFact]) -> tuple[FinanceMetric, ...]:
    result: list[FinanceMetric] = []
    metric_values = {item.value for item in FinanceMetricCode}
    for fact in facts:
        key = fact.anchor.field_key
        if key not in metric_values or not fact.anchor.period_identity:
            continue
        period_parts = fact.anchor.period_identity.split(":")
        if period_parts[0] == "YEAR":
            period = FinancePeriod(kind=FinancePeriodKind.YEAR, year=int(period_parts[1]))
        elif period_parts[0] == "QUARTER":
            period = FinancePeriod(kind=FinancePeriodKind.QUARTER, year=int(period_parts[1]), quarter=int(period_parts[2].removeprefix("Q")))
        else:
            period = FinancePeriod(kind=FinancePeriodKind.DATE, on_date=date.fromisoformat(period_parts[1]))
        value = fact.selected_evidence.value
        amount = value.get("value") if isinstance(value, dict) else value
        currency = value.get("currency") if isinstance(value, dict) else None
        result.append(
            FinanceMetric(
                fact_ref=fact.fact_ref,
                period=period,
                metric=FinanceMetricCode(key),
                value=Decimal(str(amount)),
                currency=currency,
                source=fact.selected_evidence.source_code,
                source_data_date=fact.selected_evidence.source_data_date,
                retrieved_at=fact.selected_evidence.retrieved_at,
                confidence=fact.selected_evidence.confidence,
                freshness=fact.selected_evidence.freshness,
                limitations=fact.selected_evidence.limitations,
                state=fact.state,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.period.identity, item.metric.value)))


def build_company_view_v1(
    cursor: Any,
    *,
    company_id: int,
    audience: Audience = Audience.INTERNAL,
    generated_at: datetime | None = None,
) -> CompanyViewModelV1:
    now = _aware(generated_at)
    company = _one(cursor, "SELECT inn FROM companies WHERE id=%s", (company_id,))
    if company is None:
        raise ValueError(f"company_id {company_id} is not resolved")
    facts = select_semantic_facts(load_semantic_candidates(cursor, company_id=company_id), observed_at=now)
    risk_ref, summary_ref = _risk_summary_refs(cursor, company_id)
    facts_by_section: dict[str, list[SemanticFact]] = defaultdict(list)
    for fact in facts:
        facts_by_section[fact.anchor.section_key].append(fact)
    sections = tuple(
        CompanyViewSectionV1(
            section_key=key,
            state=(
                DataState.CONFLICTING_EVIDENCE
                if any(item.state == DataState.CONFLICTING_EVIDENCE for item in facts_by_section[key])
                else DataState.FOUND
                if facts_by_section[key]
                else DataState.NOT_CHECKED
            ),
            facts=tuple(facts_by_section[key]),
        )
        for key in SECTION_KEYS
    )
    revision_input = {
        "company_id": company_id,
        "facts": [
            {
                "anchor": fact.anchor.model_dump(mode="json"),
                "selected_evidence": fact.selected_evidence.model_dump(mode="json"),
                "alternative_evidence": [
                    item.model_dump(mode="json") for item in fact.alternative_evidence
                ],
                "state": fact.state.value,
                "rights": fact.rights.value,
            }
            for fact in facts
        ],
        "risk_ref": risk_ref,
        "summary_ref": summary_ref,
    }
    revision = "cv1:" + hashlib.sha256(_canonical(revision_input).encode("utf-8")).hexdigest()
    internal = CompanyViewModelV1(
        revision=revision,
        generated_at=now,
        audience=Audience.INTERNAL,
        company_id=company_id,
        inn=str(company["inn"]),
        sections=sections,
        finances=_finance_metrics(facts),
        risk_ref=risk_ref,
        summary_ref=summary_ref,
        links={"public_card": f"/companies/{company['inn']}", "public_api": f"/api/company/{company['inn']}"},
    )
    return filter_company_view(internal, audience=audience)


def _allowed(rights: FactRights, audience: Audience) -> bool:
    if audience == Audience.INTERNAL:
        return True
    if audience == Audience.AUTHENTICATED:
        return rights != FactRights.INTERNAL_ONLY
    return rights == FactRights.PUBLIC


def filter_company_view(view: CompanyViewModelV1, *, audience: Audience) -> CompanyViewModelV1:
    sections: list[CompanyViewSectionV1] = []
    allowed_refs: set[str] = set()
    for section in view.sections:
        facts = []
        for fact in section.facts:
            if not _allowed(fact.rights, audience):
                continue
            alternatives = tuple(
                evidence for evidence in fact.alternative_evidence if _allowed(evidence.rights, audience)
            )
            anchor = fact.anchor.model_copy(update={"company_id": None}) if audience == Audience.PUBLIC else fact.anchor
            filtered = fact.model_copy(update={"anchor": anchor, "alternative_evidence": alternatives})
            facts.append(filtered)
            allowed_refs.add(filtered.fact_ref)
        state = section.state if facts else DataState.NOT_CHECKED
        sections.append(section.model_copy(update={"state": state, "facts": tuple(facts)}))
    finances = tuple(item for item in view.finances if item.fact_ref in allowed_refs)
    return view.model_copy(
        update={
            "audience": audience,
            "company_id": None if audience == Audience.PUBLIC else view.company_id,
            "sections": tuple(sections),
            "finances": finances,
        }
    )


def persist_semantic_facts(session: Session, facts: Iterable[SemanticFact]) -> int:
    count = 0
    for fact in facts:
        if fact.anchor.company_id is None:
            raise ValueError("public-filtered facts cannot be persisted")
        payload = {
            "fact_ref": fact.fact_ref,
            "item_ref": fact.item_ref,
            "company_id": fact.anchor.company_id,
            "section_key": fact.anchor.section_key,
            "field_key": fact.anchor.field_key,
            "period_identity": fact.anchor.period_identity,
            "item_identity": fact.anchor.item_identity,
            "selected_evidence": fact.selected_evidence.model_dump(mode="json"),
            "alternative_evidence": [item.model_dump(mode="json") for item in fact.alternative_evidence],
            "evidence_history": [],
            "state": fact.state.value,
            "rights": fact.rights.value,
            "is_current": True,
            "created_at": fact.created_at,
            "updated_at": fact.updated_at,
        }
        statement = pg_insert(CompanySemanticFact).values(**payload)
        statement = statement.on_conflict_do_update(
            index_elements=[CompanySemanticFact.fact_ref],
            set_={
                "item_ref": statement.excluded.item_ref,
                "selected_evidence": statement.excluded.selected_evidence,
                "alternative_evidence": statement.excluded.alternative_evidence,
                "evidence_history": sa.case(
                    (
                        CompanySemanticFact.selected_evidence
                        != statement.excluded.selected_evidence,
                        CompanySemanticFact.evidence_history.op("||")(
                            sa.func.jsonb_build_array(
                                CompanySemanticFact.selected_evidence
                            )
                        ).op("||")(CompanySemanticFact.alternative_evidence),
                    ),
                    else_=CompanySemanticFact.evidence_history,
                ),
                "state": statement.excluded.state,
                "rights": statement.excluded.rights,
                "is_current": True,
                "updated_at": statement.excluded.updated_at,
            },
        )
        session.execute(statement)
        count += 1
    return count


def materialize_company_view_v1(
    session: Session,
    *,
    company_id: int,
    generated_at: datetime | None = None,
) -> CompanyViewModelV1:
    """Build and upsert semantic facts in the caller's existing transaction."""

    driver_connection = session.connection().connection.driver_connection
    with driver_connection.cursor(row_factory=dict_row) as cursor:
        view = build_company_view_v1(
            cursor,
            company_id=company_id,
            audience=Audience.INTERNAL,
            generated_at=generated_at,
        )
    facts = tuple(fact for section in view.sections for fact in section.facts)
    session.execute(
        sa.update(CompanySemanticFact)
        .where(
            CompanySemanticFact.company_id == company_id,
            CompanySemanticFact.is_current.is_(True),
        )
        .values(is_current=False)
    )
    persist_semantic_facts(session, facts)
    return view


def resolve_company_by_inn(
    session: Session,
    inn: str,
    *,
    audience: Audience = Audience.INTERNAL,
) -> CompanyResolutionV1:
    """Resolve an exact local INN without providers, writes, or implicit creation."""

    normalized = str(inn).strip()
    if not re.fullmatch(r"\d{10}|\d{12}", normalized):
        return CompanyResolutionV1(
            state=ResolutionState.NOT_FOUND,
            inn=normalized,
            audience=audience,
            matched_count=0,
        )
    with session.no_autoflush:
        rows = tuple(session.scalars(sa.select(Company).where(Company.inn == normalized)).all())
    if not rows:
        return CompanyResolutionV1(
            state=ResolutionState.NOT_FOUND,
            inn=normalized,
            audience=audience,
            matched_count=0,
        )
    if len(rows) > 1:
        return CompanyResolutionV1(
            state=ResolutionState.AMBIGUOUS,
            inn=normalized,
            audience=audience,
            matched_count=len(rows),
        )
    company = rows[0]
    if audience == Audience.PUBLIC and company.entity_type != "legal":
        return CompanyResolutionV1(
            state=ResolutionState.RESTRICTED,
            inn=normalized,
            audience=audience,
            matched_count=1,
        )
    return CompanyResolutionV1(
        state=ResolutionState.RESOLVED,
        inn=normalized,
        audience=audience,
        company_id=None if audience == Audience.PUBLIC else company.id,
        matched_count=1,
    )
