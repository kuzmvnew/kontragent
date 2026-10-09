"""Guarded local-only real-data preview support for retained Alan evidence.

This module never creates databases and never performs ingestion.  It exports
one existing public projection from a read-only source connection, imports it
into an explicitly disposable public database, and bootstraps an owner only in
an explicitly disposable operational clone.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import psycopg
import sqlalchemy as sa
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.workspace import (
    CustomerUser,
    Workspace,
    WorkspaceEntitlement,
    WorkspaceMembership,
)
from public_app.contracts import (
    SCHEMA_VERSION,
    CanonicalManifest,
    PublicationInfo,
    ReleaseManifest,
)
from public_app.repository import PublicRepository
from scripts.export_public_release import CANONICAL_COHORT_PATH, build_projection
from scripts.import_public_release import import_release
from scripts.public_release_common import canonical_json, sha256_file, write_checksums
from workspace_app.auth import hash_password, verify_password
from workspace_app.service import bootstrap_workspace_owner


ROOT = Path(__file__).resolve().parents[1]
INN = "0100000614"
SOURCE_DATABASE = "public_card_binding_impl_01_20260928"
OPERATIONAL_DATABASE = "alan_preview_operational_20261009"
PUBLIC_DATABASE = "alan_preview_public_20261009"
RELEASE_ID = "local-real-preview-alan-v1"
OWNER_EMAIL = "alan.preview.owner@nextcompany.local"
WORKSPACE_NAME = 'ООО «АЛАН» · LOCAL REAL PREVIEW'
CANONICAL_MAIN = "c9bdb49768dc503af8ec82bdaa97653bc7412f3b"
PREVIEW_POLICY = "local-real-preview-alan-v1"
OPERATIONAL_SCHEMA_HEAD = "b5d7f9a1c3e6"
PUBLIC_SCHEMA_HEAD = "public_0002"


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def require_preview_mode(environment: dict[str, str]) -> None:
    if not _enabled(environment.get("NEXTCOMPANY_LOCAL_REAL_PREVIEW")):
        raise ValueError("NEXTCOMPANY_LOCAL_REAL_PREVIEW=1 is required")
    if _enabled(environment.get("NEXTCOMPANY_DEMO_MODE")):
        raise ValueError("synthetic demo mode must be disabled for the real-data preview")


def _database_name(database_url: str) -> str:
    try:
        name = make_url(database_url).database
    except Exception as exc:  # pragma: no cover - SQLAlchemy owns URL parsing
        raise ValueError("invalid PostgreSQL URL") from exc
    if not name:
        raise ValueError("database URL must name a database")
    return name


def _is_local_database_url(database_url: str) -> bool:
    parsed = urlsplit(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    query = parse_qs(parsed.query)
    host = parsed.hostname or (query.get("host") or [""])[0]
    return host in {"", "localhost", "127.0.0.1", "/tmp"} or str(host).startswith("/")


def validate_database_topology(
    *, source_url: str, operational_url: str, public_url: str
) -> dict[str, str]:
    values = {
        "source": (source_url, SOURCE_DATABASE),
        "operational": (operational_url, OPERATIONAL_DATABASE),
        "public": (public_url, PUBLIC_DATABASE),
    }
    resolved: dict[str, str] = {}
    for role, (url, expected) in values.items():
        if not url:
            raise ValueError(f"{role} database URL is required")
        if not _is_local_database_url(url):
            raise ValueError(f"{role} database must use a local PostgreSQL host/socket")
        observed = _database_name(url)
        if observed != expected:
            raise ValueError(f"{role} database must be exactly {expected}, got {observed}")
        resolved[role] = observed
    if len(set(resolved.values())) != 3:
        raise ValueError("source, operational clone, and public database must be distinct")
    return resolved


def _psycopg_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def inspect_source(source_url: str) -> tuple[dict, object]:
    """Read and validate the exact retained source revision and build its projection."""

    with psycopg.connect(_psycopg_url(source_url), row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database() AS name")
            if cursor.fetchone()["name"] != SOURCE_DATABASE:
                raise ValueError("source connection resolved to an unexpected database")
            cursor.execute("SELECT version_num FROM alembic_version")
            source_schema_revision = cursor.fetchone()["version_num"]
            cursor.execute(
                """SELECT id, name, full_name, inn, kpp, ogrn, address, status,
                          master_dataset_id, master_data_date, master_authority,
                          master_source, official_registry_verified, master_provenance
                   FROM companies WHERE inn=%s""",
                (INN,),
            )
            company = cursor.fetchone()
            if not company or company["name"] != 'ООО "АЛАН"':
                raise ValueError("retained Alan company row is missing or has changed identity")
            cursor.execute(
                """SELECT s.id AS snapshot_id, s.dataset_id, s.retrieved_at,
                          s.source_as_of, s.raw_sha256, s.normalized_sha256,
                          s.normalized_path, s.source_url,
                          s.projection->'enforcements'->>'snapshot' AS enforcement_source_data_date,
                          a.stored_path AS raw_path
                   FROM firmoteka_company_snapshots s
                   JOIN firmoteka_raw_artifacts a ON a.sha256=s.raw_sha256
                   WHERE s.company_id=%s AND s.is_current=TRUE
                   ORDER BY s.retrieved_at DESC LIMIT 1""",
                (company["id"],),
            )
            snapshot = cursor.fetchone()
            if not snapshot:
                raise ValueError("current retained Firmoteka snapshot is missing")
            cursor.execute(
                """SELECT assessment_id, risk_model_version, ruleset_version,
                          calculated_at, jsonb_array_length(factors) AS factor_count,
                          jsonb_array_length(limitations) AS limitation_count
                   FROM company_risk_assessments_v3 WHERE company_id=%s
                   ORDER BY calculated_at DESC LIMIT 1""",
                (company["id"],),
            )
            risk = cursor.fetchone()
            cursor.execute(
                """SELECT summary_id, risk_assessment_id, summary_model_version,
                          projection_policy_version, generated_at
                   FROM company_summaries_v3 WHERE company_id=%s
                   ORDER BY generated_at DESC LIMIT 1""",
                (company["id"],),
            )
            summary = cursor.fetchone()
            if not risk or not summary or summary["risk_assessment_id"] != risk["assessment_id"]:
                raise ValueError("matching persisted Risk v3 and Summary v3 are required")
            cursor.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE is_current=TRUE) AS current
                   FROM company_semantic_facts WHERE company_id=%s""",
                (company["id"],),
            )
            semantic = cursor.fetchone()
            published_at = max(risk["calculated_at"], summary["generated_at"])
            publication = PublicationInfo(
                schema_version=SCHEMA_VERSION,
                release_id=RELEASE_ID,
                published_at=published_at,
                result_date=published_at.date(),
                content_updated_at=published_at,
                index_eligible=False,
            )
            projection = build_projection(cursor, INN, publication)
            projection = projection.model_copy(
                update={
                    "publication": projection.publication.model_copy(
                        update={"index_eligible": False}
                    )
                }
            )

    raw_path = Path(snapshot["raw_path"])
    normalized_path = Path(snapshot["normalized_path"])
    if not raw_path.is_file() or not normalized_path.is_file():
        raise ValueError("retained Firmoteka physical artifacts are not readable")
    with gzip.open(raw_path, "rb") as stream:
        raw_digest = hashlib.sha256(stream.read()).hexdigest()
    if raw_digest != snapshot["raw_sha256"]:
        # Some retained inventories hash the stored gzip bytes. Accept only that
        # explicitly verifiable representation as the alternate convention.
        if sha256_file(raw_path) != snapshot["raw_sha256"]:
            raise ValueError("retained Firmoteka raw artifact checksum mismatch")
    evidence = {
        "mode": "LOCAL_REAL_DATA_PREVIEW",
        "non_production": True,
        "source_database": SOURCE_DATABASE,
        "source_database_role": "READ_ONLY",
        "source_schema_revision": source_schema_revision,
        "inn": INN,
        "company": {key: _jsonable(company[key]) for key in company if key != "id"},
        "company_id_in_disposable_clone": company["id"],
        "firmoteka": {key: _jsonable(snapshot[key]) for key in snapshot},
        "risk": {key: _jsonable(risk[key]) for key in risk},
        "summary": {key: _jsonable(summary[key]) for key in summary},
        "semantic_facts": {"total": semantic["total"], "current": semantic["current"]},
        "company_view": {
            "contract_version": projection.company_view.contract_version,
            "revision": projection.company_view.revision,
            "section_count": len(projection.company_view.sections),
        },
        "limitations": [
            "Retained snapshot; no live ingestion or source refresh is performed.",
            "Risk and Summary are persisted v3 results and are not recalculated by UI code.",
            "Monitoring captures a zero-event baseline only; live checks are disabled.",
            "This retained source is not the pinned canonical real E2E revision.",
        ],
    }
    return evidence, projection


def build_bundle(source_url: str, output_root: Path) -> tuple[Path, dict]:
    implementation_head = _git_head()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", CANONICAL_MAIN, implementation_head],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError(f"preview branch must descend from canonical main {CANONICAL_MAIN}")
    evidence, projection = inspect_source(source_url)
    manifest_path = ROOT / CANONICAL_COHORT_PATH
    manifest_bytes = manifest_path.read_bytes()
    cohort = CanonicalManifest.model_validate_json(manifest_bytes)
    if INN not in {item.inn for item in cohort.entities}:
        raise ValueError("Alan is absent from the accepted public cohort")
    bundle_dir = output_root / RELEASE_ID
    bundle_dir.mkdir(parents=True, exist_ok=True)
    companies_path = bundle_dir / "companies.jsonl.gz"
    with gzip.open(companies_path, "wt", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(projection.model_dump(mode="json")).decode("utf-8") + "\n")
    release = ReleaseManifest(
        schema_version=SCHEMA_VERSION,
        release_id=RELEASE_ID,
        source_main_sha=CANONICAL_MAIN,
        cohort_manifest_path=CANONICAL_COHORT_PATH,
        cohort_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        cohort_source_main_sha=cohort.source_main_sha,
        previous_release_id=None,
        created_at=projection.publication.published_at,
        result_date=projection.publication.result_date,
        content_updated_at=projection.publication.content_updated_at,
        record_count=1,
        companies_file="companies.jsonl.gz",
    )
    (bundle_dir / "manifest.json").write_bytes(
        canonical_json(release.model_dump(mode="json")) + b"\n"
    )
    write_checksums(bundle_dir)
    evidence.update(
        {
            "canonical_main": CANONICAL_MAIN,
            "implementation_head": implementation_head,
            "release_id": RELEASE_ID,
            "cohort_manifest": CANONICAL_COHORT_PATH,
            "cohort_manifest_sha256": release.cohort_manifest_sha256,
            "projection_sha256": hashlib.sha256(
                canonical_json(projection.model_dump(mode="json"))
            ).hexdigest(),
        }
    )
    (bundle_dir / "local-preview-evidence.json").write_bytes(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n"
    )
    return bundle_dir, evidence


def import_public_bundle(public_url: str, bundle_dir: Path) -> dict:
    with psycopg.connect(_psycopg_url(public_url), row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            if cursor.fetchone()["version_num"] != PUBLIC_SCHEMA_HEAD:
                raise ValueError(f"preview public database must be at {PUBLIC_SCHEMA_HEAD}")
            cursor.execute("SELECT release_id FROM public_releases")
            foreign = {row["release_id"] for row in cursor.fetchall()} - {RELEASE_ID}
            if foreign:
                raise ValueError("preview public database contains an unrelated release")
        result = import_release(connection, bundle_dir, RELEASE_ID)
    repository = PublicRepository(public_url)
    ready, release_id, count = repository.ready()
    projection = repository.get_company(INN)
    if not ready or release_id != RELEASE_ID or count != 1 or projection is None:
        raise RuntimeError("preview public repository failed post-import validation")
    return result


def bootstrap_workspace(operational_url: str, password: str) -> dict:
    if len(password) < 16:
        raise ValueError("NEXTCOMPANY_PREVIEW_PASSWORD must contain at least 16 characters")
    engine = sa.create_engine(operational_url, pool_pre_ping=True)
    with Session(engine, expire_on_commit=False) as session:
        database = session.scalar(sa.text("SELECT current_database()"))
        if database != OPERATIONAL_DATABASE:
            raise ValueError("workspace bootstrap resolved to an unexpected database")
        schema_revision = session.scalar(sa.text("SELECT version_num FROM alembic_version"))
        if schema_revision != OPERATIONAL_SCHEMA_HEAD:
            raise ValueError(
                f"preview operational database must be at {OPERATIONAL_SCHEMA_HEAD}"
            )
        user = session.scalar(
            sa.select(CustomerUser).where(CustomerUser.email == OWNER_EMAIL)
        )
        if user is None:
            user, workspace = bootstrap_workspace_owner(
                session,
                email=OWNER_EMAIL,
                password_hash=hash_password(password),
                workspace_name=WORKSPACE_NAME,
                saved_company_limit=10,
            )
        else:
            if not verify_password(password, user.password_hash):
                raise ValueError("existing preview owner password does not match")
            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id == user.id,
                    WorkspaceMembership.status == "active",
                )
            )
            if membership is None:
                raise ValueError("existing preview owner has no active workspace")
            workspace = session.get(Workspace, membership.workspace_id)
            if workspace is None:
                raise ValueError("existing preview owner workspace is missing")
            if workspace.name != WORKSPACE_NAME:
                raise ValueError("existing preview owner belongs to an unexpected workspace")
        monitoring = session.scalar(
            sa.select(WorkspaceEntitlement).where(
                WorkspaceEntitlement.workspace_id == workspace.id,
                WorkspaceEntitlement.entitlement_key == "monitoring.enabled",
            )
        )
        if monitoring is None:
            raise RuntimeError("workspace monitoring entitlement is missing")
        monitoring.enabled = True
        monitoring.policy_version = PREVIEW_POLICY
        session.commit()
        result = {
            "owner_email": user.email,
            "workspace_id": str(workspace.id),
            "workspace_name": workspace.name,
            "monitoring_enabled": True,
        }
    engine.dispose()
    return result


def verify_preview(operational_url: str, public_url: str) -> dict:
    repository = PublicRepository(public_url)
    ready, release_id, count = repository.ready()
    projection = repository.get_company(INN)
    if not ready or release_id != RELEASE_ID or count != 1 or projection is None:
        raise RuntimeError("preview PublicRepository is not ready")
    engine = sa.create_engine(operational_url, pool_pre_ping=True)
    with engine.connect() as connection:
        schema_revision = connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        if schema_revision != OPERATIONAL_SCHEMA_HEAD:
            raise RuntimeError("preview operational schema revision changed")
        counts = connection.execute(
            sa.text(
                """SELECT
                     (SELECT count(*) FROM customer_users WHERE email=:email) AS owners,
                     (SELECT count(*) FROM monitoring_events) AS monitoring_events,
                     (SELECT count(*) FROM workspace_feed_entries) AS feed_entries,
                     (SELECT count(*) FROM workspace_reports) AS reports"""
            ),
            {"email": OWNER_EMAIL},
        ).mappings().one()
    engine.dispose()
    return {
        "mode": "LOCAL_REAL_DATA_PREVIEW",
        "non_production": True,
        "release_id": release_id,
        "record_count": count,
        "inn": projection.company.inn,
        "company_name": projection.company.name,
        "company_view_revision": projection.company_view.revision,
        "risk_status": projection.risk.public_status,
        "summary": projection.public_conclusion,
        **dict(counts),
    }
