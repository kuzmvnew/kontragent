from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.company_fact import CompanyPublicFact


def canonical_value_hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upsert_company_public_fact(
    *, company_id: int, dataset_id: int | None, fact_type: str, value: dict,
    source_code: str, source_identifier: str, source_url: str,
    publication_date=None, effective_from=None, effective_to=None,
    currentness="observed", confidence="source_asserted", evidence=None,
    observed_at=None, session=None,
) -> None:
    observed_at = observed_at or datetime.now(timezone.utc)
    values = {
        "company_id": company_id, "dataset_id": dataset_id, "fact_type": fact_type,
        "value": value, "value_hash": canonical_value_hash(value), "source_code": source_code,
        "source_identifier": source_identifier, "source_url": source_url,
        "publication_date": publication_date, "effective_from": effective_from, "effective_to": effective_to,
        "currentness": currentness, "confidence": confidence, "evidence": evidence or {},
        "observed_at": observed_at,
    }
    owned_session = session is None
    session = session or get_session()
    try:
        session.execute(insert(CompanyPublicFact).values(**values).on_conflict_do_update(
            constraint="uq_company_public_fact_evidence",
            set_={k: v for k, v in values.items() if k not in {"company_id", "fact_type", "value_hash", "source_code", "source_identifier"}},
        ))
        if owned_session:
            session.commit()
    except Exception:
        if owned_session:
            session.rollback()
        raise
    finally:
        if owned_session:
            session.close()


def sync_disclosure_profile_facts(*, company_id: int, dataset_id: int, inn: str, profile: dict, source_url: str, session=None) -> None:
    for fact_type, key, kind in (
        ("registered_address", "legal_address", "registered/legal address disclosed by issuer"),
        ("postal_address", "postal_address", "postal address disclosed by issuer"),
    ):
        address = str(profile.get(key) or "").strip()
        if not address:
            continue
        upsert_company_public_fact(
            company_id=company_id, dataset_id=dataset_id, fact_type=fact_type,
            value={"address": address}, source_code="prime_disclosure",
            source_identifier=f"{inn}:{key}", source_url=source_url,
            currentness="observed_unverified_current", confidence="source_asserted",
            evidence={"matching_method": "inn_exact", "issuer_inn": inn, "field": key},
            session=session,
        )


def get_company_public_facts(company_id: int) -> list[dict]:
    session = get_session()
    try:
        rows = session.scalars(
            select(CompanyPublicFact).where(CompanyPublicFact.company_id == company_id)
            .order_by(CompanyPublicFact.fact_type, CompanyPublicFact.observed_at.desc())
        ).all()
        return [
            {
                "fact_type": row.fact_type, "value": dict(row.value), "source": row.source_code,
                "source_identifier": row.source_identifier, "source_url": row.source_url,
                "publication_date": row.publication_date, "effective_from": row.effective_from,
                "effective_to": row.effective_to, "currentness": row.currentness,
                "confidence": row.confidence, "evidence": dict(row.evidence), "observed_at": row.observed_at,
            }
            for row in rows
        ]
    finally:
        session.close()
