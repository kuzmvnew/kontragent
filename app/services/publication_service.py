"""Canonical readiness scan and durable public-publication outbox operations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
from typing import Any
from uuid import UUID

import httpx
import psycopg
from psycopg.rows import dict_row
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun
from app.models.publication import (
    PublicProjectionPublication,
    PublicPublicationRequest,
)
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from public_app.contracts import (
    CanonicalManifest,
    ChangedCompanySummary,
    PublicationInfo,
    PublicProjection,
)
from scripts.export_public_release import build_projection
from scripts.public_release_common import semantic_projection_sha256


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COHORT_PATH = ROOT / "docs/releases/public-v1-cohort-40.json"
PUBLIC_SYNC_LOCK_ID = 0x4E435053  # "NCPS"
ACTIVE_REQUEST_STATUSES = {
    "PENDING", "BUILDING", "READY", "UPLOADING", "IMPORTING", "VERIFYING",
    "RETRY_SCHEDULED",
}
RUNNING_REQUEST_STATUSES = {"BUILDING", "READY", "UPLOADING", "IMPORTING", "VERIFYING"}
STALE_PARENT_RELEASE = "STALE_PARENT_RELEASE"
PUBLICATION_QUEUE_REBASED = "PUBLICATION_QUEUE_REBASED"
NO_PUBLIC_CHANGE = "NO_PUBLIC_CHANGE"


@dataclass(frozen=True)
class PublicationQueueNormalization:
    live_release_id: str
    active_count: int
    superseded_count: int = 0
    replacement_request_id: UUID | None = None
    dirty_count: int = 0
    no_public_change: bool = False


PUBLICATION_STATUS_LABELS = {
    "PENDING": "ОЖИДАЕТ ПУБЛИКАЦИИ",
    "RETRY_SCHEDULED": "ПОВТОР ЗАПЛАНИРОВАН",
    "SUPERSEDED": "ЗАМЕНЁН НОВОЙ ВЕРСИЕЙ",
    "PUBLISHED": "ОПУБЛИКОВАН",
    "FAILED": "ОШИБКА",
    "BUILDING": "ФОРМИРУЕТСЯ",
    "READY": "ГОТОВ К ПУБЛИКАЦИИ",
    "UPLOADING": "ЗАГРУЖАЕТСЯ",
    "IMPORTING": "ИМПОРТИРУЕТСЯ",
    "VERIFYING": "ПРОВЕРЯЕТСЯ",
    "COALESCED": "ОБЪЕДИНЁН С НОВОЙ ВЕРСИЕЙ",
}

PUBLICATION_REASON_LABELS = {
    STALE_PARENT_RELEASE: (
        "Публичный release изменился после сборки кандидата; создана актуальная версия."
    ),
    PUBLICATION_QUEUE_REBASED: (
        "Очередь публикации нормализована; создана актуальная версия."
    ),
    NO_PUBLIC_CHANGE: "Публичные данные уже соответствуют текущему состоянию.",
}


class PublicBuildError(RuntimeError):
    """A fail-closed projection, cohort or bundle validation failure."""


class PublicVpsUnavailable(RuntimeError):
    """The current public release could not be read without ambiguity."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def accepted_cohort(path: Path | None = None) -> tuple[CanonicalManifest, str]:
    cohort_path = path or Path(os.getenv("PUBLIC_RELEASE_MANIFEST", DEFAULT_COHORT_PATH))
    raw = cohort_path.read_bytes()
    manifest = CanonicalManifest.model_validate_json(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sidecar = cohort_path.with_suffix(cohort_path.suffix + ".sha256")
    if sidecar.is_file() and sidecar.read_text(encoding="utf-8").split()[0] != digest:
        raise PublicBuildError("accepted cohort manifest checksum mismatch")
    return manifest, digest


def current_main_sha() -> str:
    configured = os.getenv("PUBLIC_SYNC_MAIN_SHA", "").strip()
    value = configured or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise PublicBuildError("public sync source SHA is not a full Git SHA")
    return value


def database_url_for_psycopg(value: str | None = None) -> str:
    url = value or os.getenv("DATABASE_URL", "")
    if not url:
        raise PublicBuildError("DATABASE_URL is required")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _latest_enrichment_runs(
    session: Session, companies: list[Company]
) -> dict[int, CompanyEnrichmentRun]:
    result: dict[int, CompanyEnrichmentRun] = {}
    for company in companies:
        run = session.scalar(
            select(CompanyEnrichmentRun)
            .where(CompanyEnrichmentRun.company_id == company.id)
            .order_by(
                CompanyEnrichmentRun.created_at.desc(),
                CompanyEnrichmentRun.id.desc(),
            )
            .limit(1)
        )
        if run is not None:
            result[company.id] = run
    return result


def _run_is_current_and_public_ready(
    session: Session, run: CompanyEnrichmentRun
) -> bool:
    if not (
        run.status == "succeeded"
        and run.stage == "complete"
        and run.public_ready
        and run.risk_assessment_id
        and run.summary_id
    ):
        return False
    risk = session.scalar(
        select(CompanyRiskAssessmentV3)
        .where(CompanyRiskAssessmentV3.company_id == run.company_id)
        .order_by(
            CompanyRiskAssessmentV3.calculated_at.desc(),
            CompanyRiskAssessmentV3.id.desc(),
        )
        .limit(1)
    )
    if risk is None or risk.assessment_id != run.risk_assessment_id:
        return False
    summary = session.scalar(
        select(CompanySummaryV3)
        .where(CompanySummaryV3.company_id == run.company_id)
        .order_by(
            CompanySummaryV3.generated_at.desc(), CompanySummaryV3.id.desc()
        )
        .limit(1)
    )
    return bool(
        summary
        and summary.summary_id == run.summary_id
        and summary.risk_assessment_id == risk.assessment_id
    )


def cohort_companies(session: Session, manifest: CanonicalManifest) -> list[Company]:
    inns = [entity.inn for entity in manifest.entities]
    rows = list(session.scalars(select(Company).where(Company.inn.in_(inns))))
    by_inn = {row.inn: row for row in rows}
    missing = sorted(set(inns) - set(by_inn))
    if missing:
        raise PublicBuildError(f"accepted cohort missing from Master: {', '.join(missing)}")
    return [by_inn[inn] for inn in inns]


def publishable_runs(
    session: Session, companies: list[Company]
) -> dict[int, CompanyEnrichmentRun]:
    latest = _latest_enrichment_runs(session, companies)
    return {
        company_id: run
        for company_id, run in latest.items()
        if _run_is_current_and_public_ready(session, run)
    }


def _public_client(client: httpx.Client | None = None) -> httpx.Client:
    return client or httpx.Client(
        base_url=os.getenv("PUBLIC_ORIGIN", "https://nextcompany.pro").rstrip("/"),
        timeout=httpx.Timeout(12.0, connect=5.0),
        follow_redirects=False,
        headers={"User-Agent": "nextcompany-public-sync/1"},
    )


def _validated_ready_release_id(
    http: httpx.Client,
    *,
    expected_record_count: int,
) -> str:
    ready = http.get("/api/ready")
    if ready.status_code != 200:
        raise PublicVpsUnavailable(f"public ready returned HTTP {ready.status_code}")
    payload = ready.json()
    release_id = str(payload.get("release_id") or "")
    if (
        payload.get("status") != "ready"
        or int(payload.get("record_count") or 0) != expected_record_count
        or not release_id
    ):
        raise PublicVpsUnavailable("public ready response does not match accepted cohort")
    return release_id


def fetch_live_release_id(
    manifest: CanonicalManifest,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Read and validate the actual HTTPS publication parent."""

    owns_client = client is None
    http = _public_client(client)
    try:
        return _validated_ready_release_id(
            http,
            expected_record_count=len(manifest.entities),
        )
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, PublicVpsUnavailable):
            raise
        raise PublicVpsUnavailable(type(error).__name__) from error
    finally:
        if owns_client:
            http.close()


def fetch_live_cohort(
    manifest: CanonicalManifest,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, dict[str, PublicProjection]]:
    owns_client = client is None
    http = _public_client(client)
    try:
        release_id = _validated_ready_release_id(
            http,
            expected_record_count=len(manifest.entities),
        )
        projections: dict[str, PublicProjection] = {}
        for entity in manifest.entities:
            response = http.get(f"/api/company/{entity.inn}")
            if response.status_code != 200:
                raise PublicVpsUnavailable(
                    f"public card {entity.inn} returned HTTP {response.status_code}"
                )
            projection = PublicProjection.model_validate(response.json())
            if projection.company.inn != entity.inn:
                raise PublicVpsUnavailable(f"public card identity mismatch for {entity.inn}")
            if projection.publication.release_id != release_id:
                raise PublicVpsUnavailable(f"public card release mismatch for {entity.inn}")
            projections[entity.inn] = projection
        return release_id, projections
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, PublicVpsUnavailable):
            raise
        raise PublicVpsUnavailable(type(error).__name__) from error
    finally:
        if owns_client:
            http.close()


def bootstrap_publication_state(
    session: Session,
    manifest: CanonicalManifest,
    companies: list[Company],
    *,
    now: datetime,
    client: httpx.Client | None = None,
) -> tuple[str, int]:
    """Seed semantic hashes only from the actual active public cohort."""

    states = {
        row.company_id: row
        for row in session.scalars(
            select(PublicProjectionPublication).where(
                PublicProjectionPublication.company_id.in_([item.id for item in companies])
            )
        )
    }
    if len(states) == len(companies):
        release_ids = {row.last_published_release_id for row in states.values()}
        return (next(iter(release_ids)) if len(release_ids) == 1 else "mixed"), 0
    release_id, live = fetch_live_cohort(manifest, client=client)
    created = 0
    for company in companies:
        if company.id in states:
            continue
        session.add(
            PublicProjectionPublication(
                company_id=company.id,
                last_published_hash=semantic_projection_sha256(live[company.inn]),
                last_published_release_id=release_id,
                published_at=now,
                updated_at=now,
            )
        )
        created += 1
    session.flush()
    return release_id, created


def _projection_publication(now: datetime) -> PublicationInfo:
    return PublicationInfo(
        schema_version="public-projection-v1",
        release_id="semantic-scan",
        published_at=now,
        result_date=now.date(),
        content_updated_at=now,
        index_eligible=False,
    )


def scan_public_ready_changes(
    session: Session,
    *,
    now: datetime | None = None,
    database_url: str | None = None,
    client: httpx.Client | None = None,
    source_sha: str | None = None,
) -> dict[str, int | str]:
    """Compare only latest canonical public-ready generations against live hashes."""

    now = now or utc_now()
    manifest, _cohort_hash = accepted_cohort()
    companies = cohort_companies(session, manifest)
    active_release, bootstrapped = bootstrap_publication_state(
        session, manifest, companies, now=now, client=client
    )
    states = {
        row.company_id: row
        for row in session.scalars(
            select(PublicProjectionPublication).where(
                PublicProjectionPublication.company_id.in_([item.id for item in companies])
            )
        )
    }
    runs = publishable_runs(session, companies)
    run_ids = [run.id for run in runs.values()]
    existing = set(
        session.scalars(
            select(PublicPublicationRequest.trigger_enrichment_run_id).where(
                PublicPublicationRequest.trigger_enrichment_run_id.in_(run_ids)
            )
        )
    ) if run_ids else set()
    created_sha = source_sha or current_main_sha()
    changed = unchanged = 0
    url = database_url_for_psycopg(database_url)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            for company in companies:
                run = runs.get(company.id)
                if run is None or run.id in existing:
                    continue
                projection = build_projection(
                    cursor, company.inn, _projection_publication(now)
                )
                current_hash = semantic_projection_sha256(projection)
                previous_hash = states[company.id].last_published_hash
                is_changed = current_hash != previous_hash
                request = PublicPublicationRequest(
                    trigger_type="ENRICHMENT_READY_SCAN",
                    trigger_company_id=company.id,
                    trigger_enrichment_run_id=run.id,
                    status="PENDING" if is_changed else "SUPERSEDED",
                    projection_generation=str(run.id),
                    projection_hash=current_hash,
                    changed_company_count=1 if is_changed else 0,
                    changed_company_ids=[company.id] if is_changed else [],
                    changed_company_inns=[company.inn] if is_changed else [],
                    change_summary=[
                        {
                            "inn": company.inn,
                            "previous_hash": previous_hash,
                            "current_hash": current_hash,
                        }
                    ] if is_changed else [],
                    last_error=None if is_changed else "NO_PUBLIC_CHANGE",
                    created_main_sha=created_sha,
                    completed_at=None if is_changed else now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(request)
                changed += int(is_changed)
                unchanged += int(not is_changed)
    session.flush()
    return {
        "accepted_cohort": len(companies),
        "public_ready": len(runs),
        "bootstrapped": bootstrapped,
        "changed": changed,
        "no_public_change": unchanged,
        "active_release": active_release,
    }


def _current_semantic_changes(
    session: Session,
    *,
    manifest: CanonicalManifest,
    companies: list[Company],
    baseline_hashes: dict[str, str],
    now: datetime,
    database_url: str | None = None,
) -> list[ChangedCompanySummary]:
    """Derive dirty companies from current canonical truth, never request metadata."""

    runs = publishable_runs(session, companies)
    changes: list[ChangedCompanySummary] = []
    url = database_url_for_psycopg(database_url)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            for company in companies:
                run = runs.get(company.id)
                if run is None:
                    continue
                previous_hash = baseline_hashes[company.inn]
                projection = build_projection(
                    cursor,
                    company.inn,
                    _projection_publication(now),
                )
                current_hash = semantic_projection_sha256(projection)
                if current_hash != previous_hash:
                    changes.append(
                        ChangedCompanySummary(
                            inn=company.inn,
                            previous_hash=previous_hash,
                            current_hash=current_hash,
                        )
                    )
    return changes


def _new_replacement_request(
    session: Session,
    *,
    companies: list[Company],
    changes: list[ChangedCompanySummary],
    previous_release_id: str,
    now: datetime,
    status: str,
    source_sha: str | None = None,
) -> PublicPublicationRequest:
    by_inn = {company.inn: company.id for company in companies}
    request = PublicPublicationRequest(
        trigger_type="PUBLICATION_QUEUE_REBASE",
        status=status,
        projection_generation=f"rebase:{previous_release_id}:{now.isoformat()}",
        projection_hash=hashlib.sha256(
            json.dumps(
                [(item.inn, item.current_hash) for item in changes],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        previous_release_id=previous_release_id,
        changed_company_count=len(changes),
        changed_company_ids=[by_inn[item.inn] for item in changes],
        changed_company_inns=[item.inn for item in changes],
        change_summary=[item.model_dump(mode="json") for item in changes],
        attempt_count=1 if status == "BUILDING" else 0,
        created_main_sha=source_sha or current_main_sha(),
        build_started_at=now if status == "BUILDING" else None,
        created_at=now,
        updated_at=now,
    )
    session.add(request)
    session.flush()
    return request


def _supersede_publication_requests(
    rows: list[PublicPublicationRequest],
    *,
    now: datetime,
    reason: str,
    replacement: PublicPublicationRequest | None,
) -> None:
    for row in rows:
        row.status = "SUPERSEDED"
        row.last_error = reason
        row.next_attempt_at = None
        row.completed_at = now
        row.updated_at = now
        if replacement is not None:
            row.coalesced_into_id = replacement.id


def normalize_publication_queue(
    session: Session,
    *,
    now: datetime | None = None,
    database_url: str | None = None,
    client: httpx.Client | None = None,
    source_sha: str | None = None,
    force: bool = False,
    reason_override: str | None = None,
) -> PublicationQueueNormalization:
    """Collapse obsolete active work into one candidate based on live/current truth."""

    now = now or utc_now()
    active = list(
        session.scalars(
            select(PublicPublicationRequest)
            .where(PublicPublicationRequest.status.in_(ACTIVE_REQUEST_STATUSES))
            .order_by(
                PublicPublicationRequest.created_at,
                PublicPublicationRequest.id,
            )
            .with_for_update(skip_locked=True)
        )
    )
    if not active:
        return PublicationQueueNormalization(live_release_id="", active_count=0)

    manifest, _cohort_hash = accepted_cohort()
    live_release_id = fetch_live_release_id(manifest, client=client)
    candidate_rows = [row for row in active if row.candidate_release_id]
    stale_rows = [
        row
        for row in candidate_rows
        if row.previous_release_id != live_release_id
    ]
    mature_multiple = len(active) > 1 and any(
        row.candidate_release_id or row.status != "PENDING" for row in active
    )
    if not (force or stale_rows or mature_multiple):
        return PublicationQueueNormalization(
            live_release_id=live_release_id,
            active_count=len(active),
        )

    observed_release_id, live = fetch_live_cohort(manifest, client=client)
    companies = cohort_companies(session, manifest)
    changes = _current_semantic_changes(
        session,
        manifest=manifest,
        companies=companies,
        baseline_hashes={
            inn: semantic_projection_sha256(projection)
            for inn, projection in live.items()
        },
        now=now,
        database_url=database_url,
    )
    replacement = None
    if changes:
        replacement = _new_replacement_request(
            session,
            companies=companies,
            changes=changes,
            previous_release_id=observed_release_id,
            now=now,
            status="BUILDING",
            source_sha=source_sha,
        )
    reason = reason_override or (
        NO_PUBLIC_CHANGE
        if not changes
        else STALE_PARENT_RELEASE
        if stale_rows
        else PUBLICATION_QUEUE_REBASED
    )
    _supersede_publication_requests(
        active,
        now=now,
        reason=reason,
        replacement=replacement,
    )
    return PublicationQueueNormalization(
        live_release_id=observed_release_id,
        active_count=len(active),
        superseded_count=len(active),
        replacement_request_id=replacement.id if replacement else None,
        dirty_count=len(changes),
        no_public_change=not changes,
    )


def normalize_after_successful_publication(
    session: Session,
    *,
    published_request: PublicPublicationRequest,
    now: datetime,
    database_url: str | None = None,
    source_sha: str | None = None,
) -> PublicationQueueNormalization:
    """Discard satisfied work and preserve at most one genuinely newer diff."""

    active = list(
        session.scalars(
            select(PublicPublicationRequest)
            .where(PublicPublicationRequest.status.in_(ACTIVE_REQUEST_STATUSES))
            .order_by(
                PublicPublicationRequest.created_at,
                PublicPublicationRequest.id,
            )
            .with_for_update(skip_locked=True)
        )
    )
    release_id = published_request.published_release_id or ""
    if not active:
        return PublicationQueueNormalization(
            live_release_id=release_id,
            active_count=0,
        )

    manifest, _cohort_hash = accepted_cohort()
    companies = cohort_companies(session, manifest)
    states = {
        row.company_id: row
        for row in session.scalars(
            select(PublicProjectionPublication).where(
                PublicProjectionPublication.company_id.in_(
                    [company.id for company in companies]
                )
            )
        )
    }
    if len(states) != len(companies):
        raise PublicBuildError("published projection baseline is incomplete")
    changes = _current_semantic_changes(
        session,
        manifest=manifest,
        companies=companies,
        baseline_hashes={
            company.inn: states[company.id].last_published_hash
            for company in companies
        },
        now=now,
        database_url=database_url,
    )
    replacement = None
    if changes:
        replacement = _new_replacement_request(
            session,
            companies=companies,
            changes=changes,
            previous_release_id=release_id,
            now=now,
            status="PENDING",
            source_sha=source_sha,
        )
    _supersede_publication_requests(
        active,
        now=now,
        reason=PUBLICATION_QUEUE_REBASED if changes else NO_PUBLIC_CHANGE,
        replacement=replacement,
    )
    if replacement is None:
        for row in active:
            row.coalesced_into_id = published_request.id
    return PublicationQueueNormalization(
        live_release_id=release_id,
        active_count=len(active),
        superseded_count=len(active),
        replacement_request_id=replacement.id if replacement else None,
        dirty_count=len(changes),
        no_public_change=not changes,
    )


def recover_interrupted_requests(session: Session, *, now: datetime | None = None) -> int:
    now = now or utc_now()
    rows = list(
        session.scalars(
            select(PublicPublicationRequest)
            .where(PublicPublicationRequest.status.in_(RUNNING_REQUEST_STATUSES))
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        row.status = "RETRY_SCHEDULED"
        row.next_attempt_at = now
        row.last_error = "service restarted during publication; resuming safely"
        row.updated_at = now
    return len(rows)


def claim_next_request(
    session: Session,
    *,
    now: datetime | None = None,
    debounce_seconds: int = 120,
) -> PublicPublicationRequest | None:
    now = now or utc_now()
    debounce_seconds = min(300, max(120, debounce_seconds))
    retry = session.scalar(
        select(PublicPublicationRequest)
        .where(
            PublicPublicationRequest.status == "RETRY_SCHEDULED",
            PublicPublicationRequest.next_attempt_at <= now,
        )
        .order_by(PublicPublicationRequest.next_attempt_at, PublicPublicationRequest.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if retry is not None:
        retry.status = "BUILDING" if not retry.candidate_release_id else "READY"
        retry.attempt_count += 1
        retry.next_attempt_at = None
        retry.updated_at = now
        return retry

    leader = session.scalar(
        select(PublicPublicationRequest)
        .where(
            PublicPublicationRequest.status == "PENDING",
            PublicPublicationRequest.created_at <= now - timedelta(seconds=debounce_seconds),
        )
        .order_by(PublicPublicationRequest.created_at, PublicPublicationRequest.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if leader is None:
        return None
    followers = list(
        session.scalars(
            select(PublicPublicationRequest)
            .where(
                PublicPublicationRequest.status == "PENDING",
                PublicPublicationRequest.id != leader.id,
            )
            .with_for_update(skip_locked=True)
        )
    )
    company_ids = set(leader.changed_company_ids or [])
    company_inns = set(leader.changed_company_inns or [])
    summaries = {item["inn"]: item for item in leader.change_summary or []}
    for follower in followers:
        company_ids.update(follower.changed_company_ids or [])
        company_inns.update(follower.changed_company_inns or [])
        summaries.update({item["inn"]: item for item in follower.change_summary or []})
        follower.status = "COALESCED"
        follower.coalesced_into_id = leader.id
        follower.completed_at = now
        follower.updated_at = now
    leader.changed_company_ids = sorted(company_ids)
    leader.changed_company_inns = sorted(company_inns)
    leader.change_summary = [summaries[inn] for inn in sorted(summaries)]
    leader.changed_company_count = len(company_inns)
    leader.status = "BUILDING"
    leader.attempt_count += 1
    leader.build_started_at = now
    leader.updated_at = now
    return leader


def retry_delay_seconds(attempt_count: int) -> int:
    return min(3600, 30 * (2 ** min(max(attempt_count - 1, 0), 7)))


def schedule_retry(
    request: PublicPublicationRequest,
    error: object,
    *,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    request.status = "RETRY_SCHEDULED"
    request.next_attempt_at = now + timedelta(seconds=retry_delay_seconds(request.attempt_count))
    request.last_error = str(error)[:2000]
    request.updated_at = now


def fail_request(
    request: PublicPublicationRequest,
    error: object,
    *,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    request.status = "FAILED"
    request.last_error = str(error)[:2000]
    request.completed_at = now
    request.updated_at = now


def publication_history(session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(PublicPublicationRequest)
            .order_by(PublicPublicationRequest.created_at.desc())
            .limit(limit)
        )
    )
    return [
        {
            "id": str(row.id),
            "release_id": row.published_release_id or row.candidate_release_id,
            "created_at": row.created_at,
            "trigger": row.trigger_type,
            "changed_companies": row.changed_company_inns,
            "changed_company_count": row.changed_company_count,
            "company_count": len(accepted_cohort()[0].entities),
            "previous_release_id": row.previous_release_id,
            "build_at": row.ready_at,
            "upload_at": row.uploaded_at,
            "import_at": row.imported_at,
            "verify_at": row.verified_at,
            "rollback_at": row.rollback_completed_at,
            "status": row.status,
            "status_label": PUBLICATION_STATUS_LABELS.get(
                row.status, row.status.replace("_", " ")
            ),
            "last_error": row.last_error,
            "last_error_label": PUBLICATION_REASON_LABELS.get(
                row.last_error, row.last_error
            ),
        }
        for row in rows
    ]


def publication_dashboard(
    session: Session,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    manifest, _ = accepted_cohort()
    companies = cohort_companies(session, manifest)
    ready_count = len(publishable_runs(session, companies))
    requests = list(session.scalars(select(PublicPublicationRequest)))
    counts = Counter(row.status for row in requests)
    dirty_inns = {
        inn
        for row in requests
        if row.status in ACTIVE_REQUEST_STATUSES
        for inn in (row.changed_company_inns or [])
    }
    latest = max(requests, key=lambda row: row.created_at) if requests else None
    last_published = max(
        (row for row in requests if row.status == "PUBLISHED"),
        key=lambda row: row.completed_at or row.created_at,
        default=None,
    )
    site_ready = False
    release_id = None
    record_count = 0
    site_error = None
    http = _public_client(client)
    owns_client = client is None
    try:
        response = http.get("/api/ready")
        body = response.json() if response.status_code == 200 else {}
        site_ready = (
            response.status_code == 200
            and body.get("status") == "ready"
            and int(body.get("record_count") or 0) == len(companies)
        )
        release_id = body.get("release_id")
        record_count = int(body.get("record_count") or 0)
        if not site_ready:
            site_error = (
                f"HTTP_{response.status_code}"
                if response.status_code != 200
                else "READY_RESPONSE_MISMATCH"
            )
    except Exception as error:
        site_error = type(error).__name__
    finally:
        if owns_client:
            http.close()
    active = next(
        (row for row in sorted(requests, key=lambda item: item.created_at, reverse=True)
         if row.status in ACTIVE_REQUEST_STATUSES),
        None,
    )
    if latest and latest.status == "FAILED":
        status = "ОШИБКА"
    elif active and active.status == "RETRY_SCHEDULED":
        status = "ПОВТОР ЗАПЛАНИРОВАН"
    elif active and active.status in {"BUILDING", "READY"}:
        status = "ФОРМИРУЕТСЯ"
    elif active and active.status in {"UPLOADING", "IMPORTING"}:
        status = "ЗАГРУЖАЕТСЯ"
    elif active and active.status == "VERIFYING":
        status = "ПРОВЕРЯЕТСЯ"
    elif dirty_inns:
        status = "ОЖИДАЕТ ПУБЛИКАЦИИ"
    else:
        status = "АКТУАЛЬНО"
    return {
        "site_ready": site_ready,
        "site_error": site_error,
        "active_release": release_id,
        "record_count": record_count,
        "last_publication": last_published.completed_at if last_published else None,
        "dirty_count": len(dirty_inns),
        "cohort_count": len(companies),
        "public_ready_count": ready_count,
        "enriching_count": len(companies) - ready_count,
        "status": status,
        "last_error": latest.last_error if latest and latest.status in {"FAILED", "RETRY_SCHEDULED"} else None,
        "outbox": {
            "pending": counts["PENDING"] + counts["RETRY_SCHEDULED"],
            "coalesced": counts["COALESCED"],
            "failed": counts["FAILED"],
            "published": counts["PUBLISHED"],
        },
    }
