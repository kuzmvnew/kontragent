from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.stage15_checks import InteractiveProtectedSourceSession
from app.providers.protected_public_source import PROTECTED_SOURCES


OPEN_STATUSES = {"created", "browser_open", "challenge_required", "result_ready"}


def _serialize(row):
    return {
        "id": row.id,
        "inn": row.inn,
        "source_code": row.source_code,
        "status": row.status,
        "result": row.result,
        "source_url": row.source_url,
        "evidence": row.evidence,
        "evidence_hash": row.evidence_hash,
        "browser_metadata": row.browser_metadata,
        "created_at": row.created_at,
        "checked_at": row.checked_at,
        "closed_at": row.closed_at,
    }


def start_protected_source_session(inn: str, source_code: str):
    inn = re.sub(r"\D", "", str(inn or ""))
    if len(inn) not in {10, 12}:
        raise ValueError("Некорректный ИНН")
    definition = PROTECTED_SOURCES.get(source_code)
    if definition is None:
        raise ValueError("Неизвестный protected source")
    session = get_session()
    try:
        company_id = session.scalar(select(Company.id).where(Company.inn == inn))
        if company_id is None:
            raise ValueError("Компания отсутствует в master registry")
        row = InteractiveProtectedSourceSession(
            company_id=company_id,
            inn=inn,
            source_code=source_code,
            status="created",
            source_url=definition.source_url,
            browser_metadata={"mode": "visible_human_in_the_loop", "captcha_bypass": False},
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_protected_source_session(session_id: int, *, status: str, result: str | None = None, evidence: dict | None = None, evidence_hash: str | None = None, browser_metadata: dict | None = None):
    session = get_session()
    try:
        row = session.get(InteractiveProtectedSourceSession, session_id)
        if row is None:
            raise ValueError("Interactive session не найдена")
        definition = PROTECTED_SOURCES[row.source_code]
        if status not in {"browser_open", "challenge_required", "result_ready", "completed", "unavailable", "closed"}:
            raise ValueError("Недопустимый session status")
        if result is not None and result not in definition.allowed_results:
            raise ValueError("Недопустимый result для источника")
        if result in {"active_suspensions_not_found", "high_risk_information_not_found"} and status != "completed":
            raise ValueError("Отрицательный результат допустим только после завершённой официальной проверки")
        row.status = status
        row.result = result
        row.evidence = evidence
        row.evidence_hash = evidence_hash
        if browser_metadata is not None:
            row.browser_metadata = browser_metadata
        if status == "completed":
            row.checked_at = datetime.now(timezone.utc)
            row.closed_at = row.checked_at
        elif status == "closed":
            row.closed_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        return _serialize(row)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_latest_protected_source_check(inn: str, source_code: str):
    session = get_session()
    try:
        row = session.scalar(
            select(InteractiveProtectedSourceSession)
            .where(InteractiveProtectedSourceSession.inn == inn, InteractiveProtectedSourceSession.source_code == source_code)
            .order_by(InteractiveProtectedSourceSession.created_at.desc(), InteractiveProtectedSourceSession.id.desc())
            .limit(1)
        )
        if row is None:
            return {"checked": False, "status": "not_checked", "result": None, "source_code": source_code, "source_url": PROTECTED_SOURCES[source_code].source_url}
        return {"checked": row.status == "completed", **_serialize(row)}
    finally:
        session.close()
