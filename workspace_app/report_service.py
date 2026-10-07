"""Workspace Reports: immutable snapshot generation, access, and export."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.contracts.report import DealContext
from app.models.workspace import WorkspaceAuditEvent, WorkspaceReport
from workspace_app.service import ActionDenied, authorize, resolve_legal_company


REPORT_TYPE = "COMPANY_CHECK_V1"
REPORT_SCHEMA_VERSION = "workspace-report-v1"
DEFAULT_REPORT_LIMIT = 50
MAX_REPORT_LIMIT = 100
CSV_COLUMNS = (
    "report_id",
    "generated_at",
    "inn",
    "company_name",
    "section_key",
    "section_title",
    "field_key",
    "label",
    "state",
    "value",
    "period",
    "source",
    "source_data_date",
    "freshness",
    "limitations",
)
_CSV_ALLOWED_CONTROLS = frozenset(("\t", "\n", "\r"))
_CSV_FORMULA_PREFIXES = frozenset(("=", "+", "-", "@"))


class ReportExportValidationError(ActionDenied):
    """Customer-safe, deterministic failure for invalid export values."""

    def __init__(self) -> None:
        super().__init__(
            "report_export_invalid_value",
            "Экспорт CSV остановлен: отчёт содержит недопустимый управляющий символ.",
            status_code=422,
        )


def _validate_csv_export_text(text: str) -> None:
    for character in text:
        category = unicodedata.category(character)
        if character == "\x00" or category == "Cs":
            raise ReportExportValidationError()
        if category == "Cc" and character not in _CSV_ALLOWED_CONTROLS:
            raise ReportExportValidationError()


def _validate_csv_export_value(value: Any) -> None:
    """Reject forbidden characters anywhere in a structured CSV field."""

    if isinstance(value, str):
        _validate_csv_export_text(value)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_csv_export_text(str(key))
            _validate_csv_export_value(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_csv_export_value(child)
        return
    if isinstance(value, Enum):
        _validate_csv_export_value(value.value)
        return
    if hasattr(value, "model_dump"):
        _validate_csv_export_value(value.model_dump(mode="json"))


@dataclass(frozen=True)
class ReportListItem:
    id: UUID
    subject_inn: str
    subject_name: str
    report_type: str
    schema_version: str
    generated_at: datetime
    snapshot_sha256: str


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()


def _identity_section(payload: dict[str, Any]) -> dict[str, Any]:
    publication = payload["publication"]
    source = {
        "name": "Авторизованная карточка NEXT Company",
        "source_class": "Семантическая продуктовая проекция",
        "reference": None,
        "source_data_date": publication.get("result_date"),
        "retrieved_at": publication.get("published_at"),
        "confidence": None,
        "freshness": "UNKNOWN",
    }
    labels = {
        "name": "Наименование",
        "full_name": "Полное наименование",
        "legal_status": "Статус",
        "inn": "ИНН",
        "kpp": "КПП",
        "ogrn": "ОГРН",
        "address": "Адрес",
        "registration_date": "Дата регистрации",
        "director_name": "Руководитель",
        "director_position": "Должность руководителя",
    }
    items = []
    for field_key, value in payload["company"].items():
        if value in (None, "", [], {}):
            continue
        items.append(
            {
                "field_key": field_key,
                "label": labels.get(field_key, field_key.replace("_", " ").capitalize()),
                "period": None,
                "value": value,
                "state": "FOUND",
                "source": source,
                "limitations": [],
            }
        )
    return {
        "section_key": "identity",
        "title": "Сведения о компании",
        "state": "FOUND" if items else "NOT_FOUND",
        "items": items,
    }


def _risk_section(payload: dict[str, Any]) -> dict[str, Any]:
    assessment = payload["assessment"]
    factors = [
        *assessment.get("critical_factors", []),
        *assessment.get("attention_factors", []),
        *assessment.get("positive_factors", []),
    ]
    items = [
        {
            "field_key": "assessment",
            "label": assessment.get("title") or "Результат оценки",
            "period": assessment.get("assessment_date"),
            "value": {
                "status": assessment.get("status"),
                "explanation": assessment.get("explanation"),
            },
            "state": "FOUND",
            "source": {
                "name": "Risk NEXT Company",
                "source_class": "DERIVED",
                "reference": None,
                "source_data_date": assessment.get("assessment_date"),
                "retrieved_at": None,
                "confidence": None,
                "freshness": "UNKNOWN",
            },
            "limitations": assessment.get("limitations", []),
        }
    ]
    for index, factor in enumerate(factors, start=1):
        items.append(
            {
                "field_key": f"factor_{index}",
                "label": factor.get("title") or f"Фактор {index}",
                "period": factor.get("source_data_date"),
                "value": factor,
                "state": factor.get("current_state") or "FOUND",
                "source": {
                    "name": factor.get("source_name") or "Risk NEXT Company",
                    "source_class": "DERIVED",
                    "reference": None,
                    "source_data_date": factor.get("source_data_date"),
                    "retrieved_at": None,
                    "confidence": factor.get("confidence"),
                    "freshness": "UNKNOWN",
                },
                "limitations": [],
            }
        )
    return {
        "section_key": "risk",
        "title": "Риск и факторы внимания",
        "state": assessment.get("status") or "UNKNOWN",
        "items": items,
    }


def _summary_section(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload["summary"]
    items = [
        {
            "field_key": "short_conclusion",
            "label": "Краткий вывод",
            "period": None,
            "value": summary.get("short_conclusion"),
            "state": "FOUND",
            "source": {
                "name": "Summary NEXT Company",
                "source_class": "DERIVED",
                "reference": None,
                "source_data_date": None,
                "retrieved_at": None,
                "confidence": None,
                "freshness": "UNKNOWN",
            },
            "limitations": summary.get("limitations", []),
        },
        {
            "field_key": "recommendations",
            "label": "Рекомендации",
            "period": None,
            "value": summary.get("recommendations", []),
            "state": "FOUND" if summary.get("recommendations") else "NOT_FOUND",
            "source": {
                "name": "Summary NEXT Company",
                "source_class": "DERIVED",
                "reference": None,
                "source_data_date": None,
                "retrieved_at": None,
                "confidence": None,
                "freshness": "UNKNOWN",
            },
            "limitations": summary.get("limitations", []),
        },
    ]
    return {
        "section_key": "summary",
        "title": "Вывод и рекомендации",
        "state": "FOUND",
        "items": items,
    }


def _source_section(payload: dict[str, Any]) -> dict[str, Any]:
    items = []
    for index, source in enumerate(payload.get("sources", []), start=1):
        items.append(
            {
                "field_key": f"source_{index}",
                "label": source.get("name") or f"Источник {index}",
                "period": source.get("source_data_date"),
                "value": {
                    "status": source.get("status"),
                    "explanation": source.get("explanation"),
                    "values": source.get("values"),
                },
                "state": source.get("status") or "UNKNOWN",
                "source": {
                    "name": source.get("name") or f"Источник {index}",
                    "source_class": "PRODUCT_SOURCE",
                    "reference": None,
                    "source_data_date": source.get("source_data_date"),
                    "retrieved_at": source.get("result_date"),
                    "confidence": None,
                    "freshness": "UNKNOWN",
                },
                "limitations": [],
            }
        )
    return {
        "section_key": "source_coverage",
        "title": "Источники и покрытие",
        "state": "FOUND" if items else "NOT_CHECKED",
        "items": items,
    }


def _semantic_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    view = payload.get("view")
    sections = [_identity_section(payload)]
    if isinstance(view, dict):
        sections.extend(view.get("sections", []))
    sections.extend((_risk_section(payload), _summary_section(payload), _source_section(payload)))
    return sections


def build_report_snapshot(
    *,
    report_id: UUID,
    generated_at: datetime,
    projection: Any,
    deal_context: DealContext | dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None, str | None, str, datetime | None]:
    payload = _json_value(projection.public_payload())
    if not isinstance(payload, dict):
        raise ValueError("authorized card projection did not produce an object")
    for key in ("publication", "company", "assessment", "summary", "sources"):
        if key not in payload:
            raise ValueError(f"authorized card projection is missing {key}")

    view = payload.get("view") if isinstance(payload.get("view"), dict) else None
    view_contract = (
        str(view.get("contract_version"))
        if view is not None
        else str(getattr(projection.publication, "schema_version", "public-projection-v1"))
    )
    view_generated_at = None
    if view is not None and view.get("generated_at"):
        view_generated_at = datetime.fromisoformat(
            str(view["generated_at"]).replace("Z", "+00:00")
        )

    risk = projection.risk
    risk_ref = (
        f"risk:{risk.model_version}:{risk.ruleset_version}:"
        f"{risk.assessment_date.isoformat()}"
    )
    summary_ref = f"summary:{_json_value(projection.summary.generated_at)}"
    normalized_context = None
    if deal_context is not None:
        validated_context = (
            deal_context
            if isinstance(deal_context, DealContext)
            else DealContext.model_validate(deal_context)
        )
        normalized_context = {
            "origin": "USER_PROVIDED",
            "affects_risk_or_summary": False,
            "value": _json_value(validated_context),
        }

    limitations = []
    seen_limitations: set[bytes] = set()
    for limitation in [
        *_json_value(payload["assessment"].get("limitations", [])),
        *_json_value(payload["summary"].get("limitations", [])),
    ]:
        identity = canonical_json_bytes(limitation)
        if identity not in seen_limitations:
            limitations.append(limitation)
            seen_limitations.add(identity)
    snapshot = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "report_id": str(report_id),
        "generated_at": _json_value(generated_at),
        "subject": payload["company"],
        "company_view": {
            "contract_version": view_contract,
            "revision": view.get("revision") if view else None,
            "generated_at": view.get("generated_at") if view else None,
        },
        "risk_ref": risk_ref,
        "summary_ref": summary_ref,
        "assessment": {
            **payload["assessment"],
            "model_version": risk.model_version,
            "ruleset_version": risk.ruleset_version,
        },
        "summary": payload["summary"],
        "sections": _semantic_sections(payload),
        "sources": payload["sources"],
        "limitations": limitations,
        "recommendations": payload["summary"].get("recommendations", []),
        "source_projection": {
            "schema_version": getattr(
                projection.publication, "schema_version", "public-projection-v1"
            ),
            "result_date": payload["publication"].get("result_date"),
            "content_updated_at": payload["publication"].get("content_updated_at"),
        },
        "deal_context": normalized_context,
    }
    return (
        _json_value(snapshot),
        risk_ref,
        summary_ref,
        view_contract,
        view_generated_at,
    )


def generate_report(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
    projection_repository: Any,
    deal_context: DealContext | dict[str, Any] | None = None,
) -> WorkspaceReport:
    context = authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="report.generate",
        lock_entitlement=True,
    )
    company = resolve_legal_company(session, inn)
    if context.quota_limit is not None:
        used = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkspaceReport)
                .where(WorkspaceReport.workspace_id == workspace_id)
            )
            or 0
        )
        if used >= context.quota_limit:
            session.add(
                WorkspaceAuditEvent(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_user_id=user_id,
                    action="report.generate",
                    target_type="company",
                    target_ref=company.inn,
                    outcome="quota_exceeded",
                )
            )
            session.flush()
            raise ActionDenied("quota_exceeded", "Достигнут лимит сохранённых отчётов.")

    try:
        projection = projection_repository.get_company(company.inn)
        if projection is None or projection.company.inn != company.inn:
            raise ValueError("authorized company card projection is unavailable")
        report_id = uuid4()
        generated_at = datetime.now(timezone.utc)
        snapshot, risk_ref, summary_ref, view_contract, view_generated_at = (
            build_report_snapshot(
                report_id=report_id,
                generated_at=generated_at,
                projection=projection,
                deal_context=deal_context,
            )
        )
        digest = snapshot_sha256(snapshot)
    except ActionDenied:
        raise
    except Exception as exc:
        raise ActionDenied(
            "snapshot_generation_failed",
            "Не удалось сформировать снимок отчёта.",
            status_code=409,
        ) from exc

    report = WorkspaceReport(
        id=report_id,
        workspace_id=workspace_id,
        company_id=company.id,
        generated_by_user_id=user_id,
        report_type=REPORT_TYPE,
        schema_version=REPORT_SCHEMA_VERSION,
        subject_inn=company.inn,
        subject_name=str(snapshot["subject"]["name"]),
        generated_at=generated_at,
        company_view_contract_version=view_contract,
        company_view_generated_at=view_generated_at,
        risk_ref=risk_ref,
        summary_ref=summary_ref,
        snapshot=snapshot,
        snapshot_sha256=digest,
    )
    session.add(report)
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=user_id,
            action="report.generate",
            target_type="workspace_report",
            target_ref=str(report.id),
            outcome="success",
        )
    )
    session.flush()
    return report


def list_reports(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    limit: int = DEFAULT_REPORT_LIMIT,
) -> tuple[ReportListItem, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="report.view",
    )
    bounded_limit = max(1, min(int(limit), MAX_REPORT_LIMIT))
    reports = session.scalars(
        sa.select(WorkspaceReport)
        .where(WorkspaceReport.workspace_id == workspace_id)
        .order_by(WorkspaceReport.generated_at.desc(), WorkspaceReport.id.desc())
        .limit(bounded_limit)
    ).all()
    return tuple(
        ReportListItem(
            id=report.id,
            subject_inn=report.subject_inn,
            subject_name=report.subject_name,
            report_type=report.report_type,
            schema_version=report.schema_version,
            generated_at=report.generated_at,
            snapshot_sha256=report.snapshot_sha256,
        )
        for report in reports
    )


def report_generation_state(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> tuple[bool, str | None, int | None, int, int | None]:
    try:
        context = authorize(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            permission_key="report.generate",
        )
    except ActionDenied as exc:
        return False, exc.code, None, 0, None
    used = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkspaceReport)
            .where(WorkspaceReport.workspace_id == workspace_id)
        )
        or 0
    )
    remaining = (
        None
        if context.quota_limit is None
        else max(context.quota_limit - used, 0)
    )
    if remaining == 0:
        return False, "quota_exceeded", context.quota_limit, used, remaining
    return True, None, context.quota_limit, used, remaining


def get_report(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    report_id: UUID,
    permission_key: str = "report.view",
) -> WorkspaceReport:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key=permission_key,
    )
    report = session.scalar(
        sa.select(WorkspaceReport).where(
            WorkspaceReport.id == report_id,
            WorkspaceReport.workspace_id == workspace_id,
        )
    )
    if report is None:
        raise ActionDenied(
            "report_not_found",
            "Отчёт не найден.",
            status_code=404,
        )
    if snapshot_sha256(report.snapshot) != report.snapshot_sha256:
        raise ActionDenied(
            "snapshot_integrity_failed",
            "Целостность снимка отчёта не подтверждена.",
            status_code=409,
        )
    return report


def _csv_safe_text(value: Any) -> str:
    """Serialize one CSV field, reject forbidden controls, and neutralize formulas."""

    if value is None:
        text = ""
    elif isinstance(value, (dict, list, tuple)):
        _validate_csv_export_value(value)
        try:
            text = canonical_json_bytes(value).decode("utf-8")
        except UnicodeError:
            raise ReportExportValidationError() from None
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)

    _validate_csv_export_text(text)

    for character in text:
        category = unicodedata.category(character)
        if character.isspace() or category == "Cf":
            continue
        if character in _CSV_FORMULA_PREFIXES:
            return "'" + text
        break
    return text


def report_csv_bytes(snapshot: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=CSV_COLUMNS,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    common = {
        "report_id": snapshot["report_id"],
        "generated_at": snapshot["generated_at"],
        "inn": snapshot["subject"].get("inn"),
        "company_name": snapshot["subject"].get("name"),
    }
    for section in snapshot.get("sections", []):
        for fact in section.get("items", []):
            source = fact.get("source") or {}
            if not isinstance(source, dict):
                source = {"name": source}
            row = {
                **common,
                "section_key": section.get("section_key"),
                "section_title": section.get("title"),
                "field_key": fact.get("field_key"),
                "label": fact.get("label"),
                "state": fact.get("state"),
                "value": fact.get("value"),
                "period": fact.get("period"),
                "source": source.get("name"),
                "source_data_date": source.get("source_data_date"),
                "freshness": source.get("freshness"),
                "limitations": fact.get("limitations", []),
            }
            writer.writerow(
                {key: _csv_safe_text(row.get(key)) for key in CSV_COLUMNS}
            )
    # UTF-8 BOM is intentional for Russian-language spreadsheet compatibility.
    return output.getvalue().encode("utf-8-sig")


def report_filename(report: WorkspaceReport, extension: str) -> str:
    generated_date = report.generated_at.astimezone(timezone.utc).date().isoformat()
    return (
        f"next-company-report-{report.subject_inn}-{generated_date}-"
        f"{report.id}.{extension}"
    )


def report_list_payload(item: ReportListItem) -> dict[str, Any]:
    return {
        "report_id": str(item.id),
        "subject": {"inn": item.subject_inn, "name": item.subject_name},
        "report_type": item.report_type,
        "schema_version": item.schema_version,
        "generated_at": _json_value(item.generated_at),
        "snapshot_sha256": item.snapshot_sha256,
    }
