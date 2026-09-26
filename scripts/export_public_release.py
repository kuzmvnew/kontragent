#!/usr/bin/env python3
"""Build a strict public release bundle from persisted operational data only."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from public_app.contracts import (
    SCHEMA_VERSION,
    CanonicalManifest,
    CompanyInfo,
    Freshness,
    PublicationInfo,
    PublicProjection,
    PublicRisk,
    PublicRiskFactor,
    PublicSourceBlock,
    PublicState,
    PublicSummary,
    ReleaseManifest,
    strongest_state,
)
from scripts.public_release_common import canonical_json, write_checksums

SOURCE_NAMES = {
    "REVEXP": "Доходы и расходы по данным ФНС",
    "PAYTAX": "Уплаченные налоги и сборы по данным ФНС",
    "DEBTAM": "Налоговая задолженность по данным ФНС",
    "TAXOFFENCE": "Налоговые правонарушения по данным ФНС",
}
SOURCE_CODE_NAMES = {
    "fns_tax_debt": "Налоговая задолженность ФНС",
    "fns_tax_offence": "Налоговые правонарушения ФНС",
    "firmoteka_registration": "Регистрационные сведения",
    "fssp_direct_historical_acceptance": "ФССП",
}
SOURCE_CHECK_CODES = {
    "REVEXP": "finance",
    "DEBTAM": "tax_debt",
    "TAXOFFENCE": "tax_offence",
}
CANONICAL_COHORT_PATH = "docs/releases/public-v1-cohort-40.json"


def _json_default(value: Any):
    if isinstance(value, (datetime, date, Decimal)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    raise TypeError(type(value).__name__)


def _date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _aware(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def _clean_reason(value: str) -> str:
    value = re.sub(r"\s*[—-]\s*\d+(?:[.,]\d+)?\s*балл\w*", "", str(value)).strip()
    value = re.sub(r"^Индекс риска:[^.]+\.\s*", "", value).strip()
    return value[:1000]


def _registration(risk: dict) -> dict:
    for check in risk.get("normalized_results") or risk.get("evidence_snapshot") or ():
        if check.get("check_code") == "registration" or check.get("capability_code") == "registration":
            evidence = check.get("evidence") or ()
            if evidence and isinstance(evidence[0], dict):
                value = evidence[0].get("value")
                if isinstance(value, dict):
                    return value
            payload = check.get("fact_payload")
            if isinstance(payload, dict):
                return payload
    return {}


def _director(cursor, company_id: int, risk: dict) -> tuple[str | None, str | None]:
    cursor.execute(
        """SELECT full_name, position FROM company_managers
           WHERE company_id=%s AND is_current=TRUE
           ORDER BY id DESC LIMIT 1""",
        (company_id,),
    )
    row = cursor.fetchone()
    if row and row.get("full_name"):
        return row["full_name"], row.get("position")
    for check in risk.get("normalized_results") or ():
        if check.get("check_code") != "management":
            continue
        for evidence in check.get("evidence") or ():
            value = evidence.get("value") or {}
            if isinstance(value, dict):
                name = value.get("director_name") or value.get("name") or value.get("full_name")
                position = value.get("director_position") or value.get("position")
                if name:
                    return str(name)[:600], str(position)[:300] if position else None
                for key in ("leaders", "directors", "management", "managers"):
                    candidates = value.get(key)
                    if not isinstance(candidates, list):
                        continue
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            continue
                        name = candidate.get("name") or candidate.get("full_name")
                        position = candidate.get("position") or candidate.get("title")
                        if name:
                            return str(name)[:600], str(position)[:300] if position else None
    return None, None


def _latest_json(cursor, table: str, company_id: int, order: str) -> dict | None:
    cursor.execute(
        f"SELECT to_jsonb(row_value) AS value FROM (SELECT * FROM {table} WHERE company_id=%s ORDER BY {order} LIMIT 1) row_value",
        (company_id,),
    )
    row = cursor.fetchone()
    return row["value"] if row else None


def _dataset(cursor, dataset_id: int | None) -> dict:
    if dataset_id is None:
        return {}
    cursor.execute("SELECT to_jsonb(d) AS value FROM data_sets d WHERE id=%s", (dataset_id,))
    row = cursor.fetchone()
    return row["value"] if row else {}


def _freshness(dataset: dict, result_date: date) -> Freshness:
    actual_until = _date(dataset.get("official_actual_until"))
    if actual_until and actual_until < result_date:
        return Freshness.STALE
    if dataset.get("last_data_date") or dataset.get("source_as_of"):
        return Freshness.CURRENT
    return Freshness.UNKNOWN


def _risk_check(risk: dict, code: str) -> dict | None:
    capability = SOURCE_CHECK_CODES.get(code)
    if not capability:
        return None
    for check in risk.get("normalized_results") or risk.get("resolved_checks") or ():
        if check.get("check_code") == capability or check.get("capability_code") == capability:
            return check
    return None


def _check_state(check: dict | None) -> PublicState | None:
    if not check:
        return None
    execution = str(check.get("execution") or "").upper()
    result = str(check.get("result") or check.get("observation") or "").upper()
    resolution = str(check.get("resolution_state") or "").upper()
    freshness = str(check.get("freshness") or "").upper()
    if resolution == "CONFLICTING_EVIDENCE":
        return PublicState.CONFLICTING_EVIDENCE
    if execution in {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "NOT_CHECKED"}:
        return PublicState(execution)
    if result == "UNAVAILABLE":
        return PublicState.SOURCE_UNAVAILABLE
    if result in {"FOUND", "NOT_FOUND", "NOT_APPLICABLE", "PARTIAL", "UNKNOWN"}:
        state = PublicState(result)
    else:
        state = PublicState.UNKNOWN
    if freshness == "STALE":
        return PublicState.STALE_DATA
    return state


def _check_source_date(check: dict | None) -> date | None:
    if not check:
        return None
    direct = _date(check.get("source_as_of") or check.get("effective_at"))
    if direct:
        return direct
    for evidence in check.get("evidence") or ():
        value = evidence.get("value") if isinstance(evidence, dict) else None
        if isinstance(value, dict):
            candidate = _date(value.get("data_date") or value.get("source_data_date"))
            if candidate:
                return candidate
    return None


def _source_block(
    cursor, code: str, company_id: int, risk: dict, default_result_date: date
) -> PublicSourceBlock:
    check = _risk_check(risk, code)
    checked_state = _check_state(check)
    checked_result_date = _date(check.get("checked_at")) if check else None
    if code == "REVEXP":
        row = _latest_json(cursor, "company_revenue_expense_snapshots", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Год": row.get("data_year"), "Выручка, ₽": _money(row.get("revenue")),
            "Расходы, ₽": _money(row.get("expenses")), "Прибыль / убыток, ₽": _money(row.get("profit_loss")),
        }
    elif code == "PAYTAX":
        row = _latest_json(cursor, "company_tax_payment_snapshots", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Год": row.get("data_year"), "Всего уплачено, ₽": _money(row.get("total_amount")),
            "Налоги, ₽": _money(row.get("tax_amount")), "Страховые взносы, ₽": _money(row.get("insurance_amount")),
            "Пени, ₽": _money(row.get("penalty_amount")),
        }
    elif code == "DEBTAM":
        row = _latest_json(cursor, "company_tax_debt_snapshots", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Общая задолженность, ₽": _money(row.get("total_debt")), "Недоимка, ₽": _money(row.get("total_arrears")),
            "Пени, ₽": _money(row.get("total_penalties")), "Штрафы, ₽": _money(row.get("total_fines")),
        }
    else:
        row = _latest_json(cursor, "company_tax_offences", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Сумма штрафа, ₽": _money(row.get("fine_amount")),
            "Дата документа": str(row.get("document_date")) if row.get("document_date") else None,
        }
    if not row:
        state = checked_state or PublicState.UNKNOWN
        limitation = (
            "Совпадение не найдено в опубликованном наборе данных за указанную дату; вывод ограничен охватом этого набора."
            if state == PublicState.NOT_FOUND
            else str((check or {}).get("limitation") or "В сохранённых данных нет результата, достаточного для положительного или отрицательного вывода.")
        )
        check_freshness = str((check or {}).get("freshness") or "").upper()
        freshness = Freshness.STALE if state == PublicState.STALE_DATA else (
            Freshness.CURRENT if check_freshness == "CURRENT" else Freshness.UNKNOWN
        )
        return PublicSourceBlock(
            code=code, state=state, values={}, source_name=SOURCE_NAMES[code],
            source_data_date=_check_source_date(check),
            result_date=checked_result_date or default_result_date,
            freshness=freshness, limitation=limitation,
        )
    dataset = _dataset(cursor, row.get("dataset_id"))
    row_result_date = _date(row.get("updated_at") or row.get("created_at")) or default_result_date
    freshness = _freshness(dataset, default_result_date)
    row_state = PublicState.STALE_DATA if freshness == Freshness.STALE else PublicState.FOUND
    if checked_state == PublicState.NOT_FOUND:
        state = PublicState.CONFLICTING_EVIDENCE
    else:
        state = strongest_state(row_state, checked_state) if checked_state else row_state
    if state == PublicState.STALE_DATA:
        freshness = Freshness.STALE
    limitation = None
    if state == PublicState.STALE_DATA:
        limitation = "Срок актуальности опубликованного набора данных истёк; значение показано как историческое."
    elif state == PublicState.CONFLICTING_EVIDENCE:
        limitation = "Сохранённые результаты источника противоречат друг другу; требуется повторная проверка."
    elif state not in {PublicState.FOUND, PublicState.NOT_FOUND, PublicState.NOT_APPLICABLE}:
        limitation = str((check or {}).get("limitation") or "Результат источника имеет ограничение и не допускает положительного вывода.")
    return PublicSourceBlock(
        code=code, state=state, values=values,
        source_name=str(dataset.get("name") or SOURCE_NAMES[code])[:250],
        source_data_date=_date(row.get("data_date")),
        result_date=checked_result_date or row_result_date,
        freshness=freshness, limitation=limitation,
    )


def _risk_projection(risk: dict) -> PublicRisk:
    result_payload = risk.get("result_payload") or {}
    raw_factors = risk.get("factors") or result_payload.get("points") or ()
    factors = []
    for factor in raw_factors[:12]:
        title = factor.get("title") or factor.get("fact") or factor.get("factor_code")
        if not title:
            continue
        source_code = factor.get("source_code")
        factors.append(
            PublicRiskFactor(
                title=_clean_reason(str(title)),
                explanation=str(factor.get("explanation"))[:2000] if factor.get("explanation") else None,
                source_name=SOURCE_CODE_NAMES.get(source_code, str(source_code) if source_code else None),
                source_data_date=_date(factor.get("source_as_of") or factor.get("effective_at")),
            )
        )
    coverage = risk.get("coverage_snapshot") or risk.get("coverage") or result_payload.get("coverage") or {}
    limitations = []
    for item in risk.get("limitations") or ():
        if isinstance(item, str):
            limitations.append(item[:1500])
        elif isinstance(item, dict):
            text = item.get("explanation") or item.get("message") or item.get("limitation_code")
            if text:
                limitations.append(str(text)[:1500])
    is_partial = bool(result_payload.get("preliminary")) or not bool(coverage.get("mandatory_hard_checks_resolved", True)) or bool(limitations)
    state = PublicState.PARTIAL if is_partial else (PublicState.FOUND if factors else PublicState.NOT_FOUND)
    if state == PublicState.PARTIAL:
        title = "Оценка содержит ограничения"
    elif factors:
        title = "Выявлены факторы, требующие внимания"
    else:
        title = "Неблагоприятные факторы не выявлены в выполненных проверках"
    explanation = (
        "Факторы приведены по сохранённому результату Risk v3. Ограничения полноты не позволяют трактовать отсутствие отдельных сведений как отсутствие риска."
        if is_partial else
        "Факторы приведены по сохранённому результату Risk v3 без рейтинга или вероятностной интерпретации."
    )
    return PublicRisk(
        state=state, title=title, explanation=explanation, factors=tuple(factors),
        limitations=tuple(dict.fromkeys(limitations)),
        assessment_date=_aware(risk["calculated_at"]).date(),
        model_version=risk.get("risk_model_version") or risk.get("risk_engine_version"),
        ruleset_version=risk.get("ruleset_version"),
    )


def _summary_projection(summary: dict, public_risk: PublicRisk) -> PublicSummary:
    payload = summary.get("structured_payload") or {}
    main = tuple(_clean_reason(value) for value in payload.get("main_reasons") or () if _clean_reason(value))
    limitations = tuple(str(value)[:1500] for value in payload.get("limitations") or ())
    recommendations = tuple(str(value)[:1500] for value in payload.get("recommendations") or ())
    if public_risk.factors:
        conclusion = "Выявлены факторы, требующие внимания. Их значение следует оценивать вместе с полнотой и датами исходных данных."
    elif public_risk.state == PublicState.NOT_FOUND:
        conclusion = "Неблагоприятные факторы не выявлены в рамках выполненных проверок."
    else:
        conclusion = "Данных недостаточно для положительного вывода; ограничения проверки указаны ниже."
    return PublicSummary(
        short_conclusion=conclusion, main_factors=main,
        limitations=limitations, recommendations=recommendations,
        generated_at=_aware(summary["generated_at"]),
    )


def _load_latest(cursor, table: str, company_id: int, date_column: str) -> dict | None:
    return _latest_json(cursor, table, company_id, f"{date_column} DESC, id DESC")


def build_projection(cursor, inn: str, publication: PublicationInfo) -> PublicProjection:
    cursor.execute("SELECT to_jsonb(c) AS value FROM companies c WHERE inn=%s", (inn,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"INN {inn}: company is absent from Master")
    company = row["value"]
    if company.get("entity_type") != "legal":
        raise ValueError(f"INN {inn}: IP/non-legal entity rejected")
    risk = _load_latest(cursor, "company_risk_assessments_v3", company["id"], "calculated_at")
    if not risk:
        raise ValueError(f"INN {inn}: persisted Risk v3 is missing")
    summary = _load_latest(cursor, "company_summaries_v3", company["id"], "generated_at")
    if not summary or summary.get("risk_assessment_id") != risk.get("assessment_id"):
        raise ValueError(f"INN {inn}: matching persisted Summary v3 is missing")
    registration = _registration(risk)
    if registration.get("requested_inn") not in {None, inn}:
        raise ValueError(f"INN {inn}: registration evidence identity mismatch")
    director_name, director_position = _director(cursor, company["id"], risk)
    projection_result_date = _aware(risk["calculated_at"]).date()
    sources = tuple(
        _source_block(cursor, code, company["id"], risk, projection_result_date)
        for code in ("REVEXP", "PAYTAX", "DEBTAM", "TAXOFFENCE")
    )
    public_risk = _risk_projection(risk)
    status = registration.get("status_detail") or registration.get("status_normalized") or company.get("status")
    company_info = CompanyInfo(
        name=company.get("short_name") or company.get("name") or registration.get("name"),
        full_name=company.get("full_name") or registration.get("full_name"),
        legal_status=str(status)[:240] if status else None,
        inn=inn,
        kpp=company.get("kpp") or registration.get("kpp"),
        ogrn=company.get("ogrn") or registration.get("ogrn"),
        address=company.get("address") or registration.get("address"),
        registration_date=_date(company.get("registration_date") or registration.get("registration_date")),
        director_name=director_name,
        director_position=director_position,
    )
    index_eligible = bool(
        company_info.ogrn
        and all(source.state in {PublicState.FOUND, PublicState.NOT_FOUND} for source in sources)
    )
    content_candidates = [_aware(risk["calculated_at"]), _aware(summary["generated_at"])]
    for field in ("updated_at", "source_updated_at"):
        if company.get(field):
            content_candidates.append(_aware(company[field]))
    per_company_publication = publication.model_copy(
        update={
            "index_eligible": index_eligible,
            "content_updated_at": max(content_candidates),
            "result_date": projection_result_date,
        }
    )
    return PublicProjection(
        publication=per_company_publication,
        company=company_info,
        risk=public_risk,
        summary=_summary_projection(summary, public_risk),
        sources=sources,
    )


def release_id_for(now: datetime, source_sha: str, cohort_sha256: str) -> str:
    return (
        f"public-v1-{now.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{source_sha[:8]}-{cohort_sha256[:8]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / CANONICAL_COHORT_PATH)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--source-main-sha")
    parser.add_argument("--previous-release-id")
    parser.add_argument("--release-id")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    manifest_bytes = args.manifest.read_bytes()
    manifest = CanonicalManifest.model_validate_json(manifest_bytes)
    cohort_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_sidecar = args.manifest.with_suffix(args.manifest.suffix + ".sha256")
    if checksum_sidecar.is_file():
        recorded = checksum_sidecar.read_text(encoding="utf-8").split()[0]
        if recorded != cohort_manifest_sha256:
            raise ValueError("canonical cohort manifest checksum mismatch")
    source_sha = args.source_main_sha or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source main SHA must be a full Git SHA")
    now = datetime.now(UTC)
    release_id = args.release_id or release_id_for(now, source_sha, cohort_manifest_sha256)
    if release_id in {args.previous_release_id, manifest.supersedes_release_id}:
        raise ValueError("new cohort requires a new release ID")
    bundle_dir = args.output_root / release_id
    base_publication = PublicationInfo(
        schema_version=SCHEMA_VERSION, release_id=release_id, published_at=now,
        result_date=now.date(), content_updated_at=now, index_eligible=False,
    )
    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            projections = [build_projection(cursor, entity.inn, base_publication) for entity in manifest.entities]
    expected_count = len(manifest.entities)
    if len(projections) != expected_count:
        raise ValueError("export did not produce the exact accepted cohort")
    bundle_dir.mkdir(parents=True, exist_ok=False)
    content_updated_at = max(item.publication.content_updated_at for item in projections)
    with gzip.open(bundle_dir / "companies.jsonl.gz", "wt", encoding="utf-8", newline="\n") as stream:
        for projection in projections:
            stream.write(canonical_json(projection.model_dump(mode="json")).decode("utf-8") + "\n")
    release_manifest = ReleaseManifest(
        schema_version=SCHEMA_VERSION, release_id=release_id, source_main_sha=source_sha,
        cohort_manifest_path=CANONICAL_COHORT_PATH,
        cohort_manifest_sha256=cohort_manifest_sha256,
        cohort_source_main_sha=manifest.source_main_sha,
        previous_release_id=args.previous_release_id, created_at=now,
        result_date=max(item.publication.result_date for item in projections),
        content_updated_at=content_updated_at, record_count=expected_count,
        companies_file="companies.jsonl.gz",
    )
    (bundle_dir / "manifest.json").write_bytes(canonical_json(release_manifest.model_dump(mode="json")) + b"\n")
    write_checksums(bundle_dir)
    print(json.dumps({"release_id": release_id, "record_count": expected_count, "bundle": str(bundle_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
