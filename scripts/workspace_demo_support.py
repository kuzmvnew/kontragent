"""Safe local Demo orchestration for the NEXT Company product.

The module deliberately composes production services. It does not import test
helpers, provide an authentication shortcut, or write public projections around
the release importer.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import sqlalchemy as sa
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.models.company import Company
from app.models.monitoring import MonitoringEvent, MonitoringSubscription, WorkspaceFeedEntry
from app.models.semantic_fact import CompanySemanticFact
from app.models.workspace import (
    CustomerUser,
    SavedCompany,
    Workspace,
    WorkspaceBulkJob,
    WorkspaceEntitlement,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceReport,
    WorkspaceRole,
)
from public_app.contracts import (
    CompanyInfo,
    Freshness,
    PublicationInfo,
    PublicCompanyViewV1,
    PublicFactSource,
    PublicLimitation,
    PublicProjection,
    PublicRecommendation,
    PublicRisk,
    PublicRiskFactor,
    PublicSourceBlock,
    PublicState,
    PublicSummary,
    PublicViewFact,
    PublicViewSection,
    ReleaseManifest,
)
from public_app.repository import PublicRepository
from scripts.import_public_release import import_release
from scripts.public_release_common import canonical_json, write_checksums
from workspace_app.auth import hash_password, verify_password
from workspace_app.bulk_service import create_bulk_job, process_bulk_job_chunk
from workspace_app.member_service import accept_invitation, create_invitation
from workspace_app.monitoring_service import monitor_company_once, subscribe_company
from workspace_app.report_service import generate_report
from workspace_app.service import (
    bootstrap_workspace_owner,
    save_company,
    saved_company_for_inn,
    update_saved_company_note,
)


DEMO_MODE_ENV = "NEXTCOMPANY_DEMO_MODE"
DEMO_PASSWORD_ENV = "NEXTCOMPANY_DEMO_PASSWORD"
DEMO_EXPECTED_SHA_ENV = "NEXTCOMPANY_DEMO_EXPECTED_SHA"
DEMO_OWNER_EMAIL = "demo.owner@nextcompany.local"
DEMO_MEMBER_EMAIL = "demo.member@nextcompany.local"
DEMO_WORKSPACE_NAME = "NEXT Company Demo"
DEMO_RELEASE_ID = "nextcompany-demo-v1"
DEMO_RELEASE_PREFIX = "nextcompany-demo-"
DEMO_POLICY_VERSION = "workspace-demo-v1"
DEMO_SOURCE_NAME = "Демонстрационный набор next.company"
DEMO_SOURCE_CLASS = "DEMO_SYNTHETIC"
DEMO_RESULT_DATE = date(2026, 1, 15)
DEMO_TIMESTAMP = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
EXPECTED_OPERATIONAL_HEAD = "b5d7f9a1c3e6"
EXPECTED_PUBLIC_HEAD = "public_0002"
RESET_CONFIRMATION = "LOCAL_DEMO_ONLY"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class DemoCompany:
    inn: str
    name: str
    role: str
    status: str
    indicator: str
    advanced_indicator: str
    attention: bool = False


DEMO_COHORT = (
    DemoCompany(
        "9000000014",
        "ООО «СИНТЕТИЧЕСКАЯ ДЕМО — СТАБИЛЬНАЯ КОМПАНИЯ»",
        "stable",
        "Действует (синтетический Demo-статус)",
        "Стабильный демонстрационный профиль",
        "Профиль обновлён в демонстрационном сценарии",
    ),
    DemoCompany(
        "9000000021",
        "ООО «СИНТЕТИЧЕСКАЯ ДЕМО — ТРЕБУЕТ ВНИМАНИЯ»",
        "attention",
        "Действует (синтетический Demo-статус)",
        "Синтетический фактор требует внимания",
        "Синтетический фактор повторно оценён",
        attention=True,
    ),
    DemoCompany(
        "9000000039",
        "ООО «СИНТЕТИЧЕСКАЯ ДЕМО — ОГРАНИЧЕННЫЕ ДАННЫЕ»",
        "limited",
        "Данные ограничены (синтетический Demo-статус)",
        "Доступен ограниченный демонстрационный набор",
        "Демонстрационный набор дополнен",
    ),
    DemoCompany(
        "9000000046",
        "ООО «СИНТЕТИЧЕСКАЯ ДЕМО — МОНИТОРИНГ»",
        "monitoring",
        "Действует (синтетический Demo-статус)",
        "Базовое состояние мониторинга",
        "Изменённое состояние мониторинга",
    ),
    DemoCompany(
        "9000000053",
        "ООО «СИНТЕТИЧЕСКАЯ ДЕМО — МАССОВАЯ ПРОВЕРКА»",
        "bulk",
        "Действует (синтетический Demo-статус)",
        "Готова к демонстрации Bulk Check",
        "Bulk-профиль обновлён",
    ),
    DemoCompany(
        "9000000060",
        "ООО «СИНТЕТИЧЕСКАЯ ДЕМО — ПАРТНЁР»",
        "member",
        "Действует (синтетический Demo-статус)",
        "Профиль для совместной работы",
        "Профиль совместной работы обновлён",
    ),
)


def demo_mode_enabled(environ: dict[str, str] | os._Environ[str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(DEMO_MODE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def require_demo_mode() -> None:
    if not demo_mode_enabled():
        raise RuntimeError(f"{DEMO_MODE_ENV}=1 is required")


def _psycopg_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def validate_demo_database_url(value: str | None, *, label: str) -> str:
    if not value:
        raise ValueError(f"{label} is required")
    parsed = make_url(value)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError(f"{label} must use PostgreSQL")
    if parsed.host not in {"localhost", "127.0.0.1"}:
        raise ValueError(f"{label} must point to localhost or 127.0.0.1")
    database = str(parsed.database or "")
    if "demo" not in database.casefold():
        raise ValueError(f"{label} database name must contain 'demo'")
    return value


def validate_demo_topology(
    *,
    operational_url: str | None,
    public_import_url: str | None,
    public_web_url: str | None = None,
) -> tuple[str, str, str | None]:
    operational = validate_demo_database_url(operational_url, label="DATABASE_URL")
    public_import = validate_demo_database_url(
        public_import_url, label="PUBLIC_IMPORT_DATABASE_URL"
    )
    public_web = (
        validate_demo_database_url(public_web_url, label="PUBLIC_DATABASE_URL")
        if public_web_url
        else None
    )
    if make_url(operational).database == make_url(public_import).database:
        raise ValueError("operational and public Demo databases must be separate")
    if public_web and make_url(public_web).database != make_url(public_import).database:
        raise ValueError("public importer and web credentials must use the same Demo database")
    return operational, public_import, public_web


def demo_password_from_environment() -> str:
    password = os.getenv(DEMO_PASSWORD_ENV, "")
    if not password:
        raise RuntimeError(f"{DEMO_PASSWORD_ENV} is required")
    # Reuse the real password policy. The resulting hash is intentionally discarded.
    hash_password(password)
    return password


def validate_demo_source_sha(value: str, *, label: str = "Demo source SHA") -> str:
    if not SOURCE_SHA_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be exactly 40 lowercase hexadecimal characters")
    return value


def resolve_demo_source_sha(
    *,
    expected_sha: str | None = None,
    repository_root: Path = REPOSITORY_ROOT,
) -> str:
    """Resolve committed local provenance and optionally assert an expected exact HEAD."""

    try:
        process = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("cannot resolve Demo source SHA from git HEAD") from exc
    if process.returncode:
        raise RuntimeError(
            "cannot resolve Demo source SHA from git HEAD: "
            + (process.stderr.strip() or "git rev-parse failed")
        )
    actual = validate_demo_source_sha(process.stdout.strip(), label="actual git HEAD")
    asserted = (
        os.environ.get(DEMO_EXPECTED_SHA_ENV)
        if expected_sha is None
        else expected_sha
    )
    if asserted is not None:
        asserted = validate_demo_source_sha(
            asserted,
            label=DEMO_EXPECTED_SHA_ENV,
        )
        if actual != asserted:
            raise RuntimeError(
                f"actual git HEAD {actual} does not match {DEMO_EXPECTED_SHA_ENV} {asserted}"
            )
    return actual


def verify_migration_head(database_url: str, *, expected: str, label: str) -> str:
    with psycopg.connect(_psycopg_url(database_url)) as connection:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    heads = tuple(str(row[0]) for row in rows)
    if heads != (expected,):
        observed = ", ".join(heads) if heads else "none"
        raise RuntimeError(
            f"{label} schema is not at expected head {expected} (observed: {observed}); "
            "run the documented Alembic upgrade explicitly"
        )
    return expected


def _stable_uuid(kind: str, inn: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://next.company/demo/{kind}/{inn}")


def _limitation() -> PublicLimitation:
    return PublicLimitation(
        headline="Синтетическая демонстрационная среда",
        short_explanation=(
            "Официальные источники не запрашивались: сведения созданы только для "
            "функциональной демонстрации продукта."
        ),
        effect_on_conclusion=(
            "Вывод нельзя использовать для проверки реального контрагента или делового решения."
        ),
        what_remains_unknown="Фактическое состояние любого реального юридического лица.",
    )


def _recommendation() -> PublicRecommendation:
    return PublicRecommendation(
        action="Для реальной проверки выполните отдельный запрос к подтверждённым источникам.",
        rationale="Demo-набор не содержит результатов официальных проверок.",
        effect="Исключает смешение функциональной демонстрации с production evidence.",
    )


def _fact_source() -> PublicFactSource:
    return PublicFactSource(
        name=DEMO_SOURCE_NAME,
        source_class=DEMO_SOURCE_CLASS,
        source_data_date=DEMO_RESULT_DATE,
        retrieved_at=DEMO_TIMESTAMP,
        confidence=1.0,
        freshness=Freshness.CURRENT,
    )


def _view_fact(company: DemoCompany, *, field_key: str, label: str, value: Any) -> PublicViewFact:
    return PublicViewFact(
        fact_ref=f"fact:{_stable_uuid('public-fact-' + field_key, company.inn)}",
        item_ref=f"item:{_stable_uuid('public-item-' + field_key, company.inn)}",
        field_key=field_key,
        label=label,
        value=value,
        state="Синтетические демонстрационные сведения",
        source=_fact_source(),
        limitations=("Не является сведением о реальном юридическом лице.",),
    )


def build_demo_projection(company: DemoCompany) -> PublicProjection:
    identity = PublicViewSection(
        section_key="identity",
        title="Основные сведения",
        state="DEMO_SYNTHETIC",
        items=(
            _view_fact(
                company,
                field_key="legal_form",
                label="Организационно-правовая форма",
                value={"name": "Демонстрационное юридическое лицо"},
            ),
        ),
    )
    activity = PublicViewSection(
        section_key="activity",
        title="Деятельность",
        state="DEMO_SYNTHETIC",
        items=(
            _view_fact(
                company,
                field_key="okved",
                label="Демонстрационный код деятельности",
                value="62.01",
            ),
            _view_fact(
                company,
                field_key="okved_name",
                label="Демонстрационная деятельность",
                value="Разработка программного обеспечения (синтетический факт)",
            ),
        ),
    )
    view_payload = {
        "inn": company.inn,
        "sections": [
            identity.model_dump(mode="json"),
            activity.model_dump(mode="json"),
        ],
    }
    revision = "cv1:" + hashlib.sha256(canonical_json(view_payload)).hexdigest()
    limitation = _limitation()
    factors: tuple[PublicRiskFactor, ...] = ()
    if company.attention:
        factors = (
            PublicRiskFactor(
                meaning_id="meaning:demo:synthetic-attention",
                category="Синтетический Demo-фактор",
                severity="Требует внимания",
                title="Синтетический демонстрационный фактор требует внимания.",
                explanation=(
                    "Фактор создан набором next.company только для показа интерфейса и сценариев."
                ),
                full_explanation=(
                    "Он не получен из ФНС или другого официального источника и не описывает "
                    "реального контрагента."
                ),
                client_meaning="Используйте фактор только для знакомства с продуктом.",
                what_it_does_not_mean="Не является оценкой реального юридического лица.",
                confidence=1.0,
                source_name=DEMO_SOURCE_NAME,
                source_data_date=DEMO_RESULT_DATE,
            ),
        )
    sources = tuple(
        PublicSourceBlock(
            code=code,
            state=PublicState.NOT_CHECKED,
            values={},
            source_name="Официальный источник не запрашивался в Demo",
            source_data_date=None,
            result_date=DEMO_RESULT_DATE,
            freshness=Freshness.UNKNOWN,
            limitation=(
                "Официальный источник не запрашивался для синтетических Demo-данных; "
                "результат проверки отсутствует."
            ),
        )
        for code in ("REVEXP", "PAYTAX", "DEBTAM", "TAXOFFENCE")
    )
    suffix = int(company.inn[-3:])
    return PublicProjection(
        publication=PublicationInfo(
            schema_version="public-projection-v1",
            release_id=DEMO_RELEASE_ID,
            published_at=DEMO_TIMESTAMP,
            result_date=DEMO_RESULT_DATE,
            content_updated_at=DEMO_TIMESTAMP,
            index_eligible=False,
        ),
        company=CompanyInfo(
            name=company.name,
            full_name=company.name,
            legal_status=company.status,
            inn=company.inn,
            kpp=f"900{suffix:03d}001",
            ogrn=f"1269000000{suffix:03d}",
            address="Демо-среда, синтетический адрес; не является реальным адресом",
            registration_date=date(2024, 1, 15),
            director_name="СИНТЕТИЧЕСКИЙ ДЕМО-РУКОВОДИТЕЛЬ",
            director_position="Демонстрационная роль",
        ),
        risk=PublicRisk(
            state=PublicState.PARTIAL,
            title="Синтетическая демонстрационная оценка",
            explanation=(
                "Оценка иллюстрирует состояние интерфейса и не основана на официальных проверках."
            ),
            factors=factors,
            limitations=(limitation,),
            assessment_date=DEMO_RESULT_DATE,
            model_version="demo-synthetic-v1",
            ruleset_version="demo-synthetic-v1",
        ),
        summary=PublicSummary(
            short_conclusion=(
                "Синтетический Demo-профиль предназначен только для функциональной приёмки."
            ),
            main_factors=((company.indicator,) if company.attention else ()),
            limitations=(limitation,),
            recommendations=(_recommendation(),),
            generated_at=DEMO_TIMESTAMP,
        ),
        sources=sources,
        company_view=PublicCompanyViewV1(
            revision=revision,
            generated_at=DEMO_TIMESTAMP,
            inn=company.inn,
            sections=(identity, activity),
        ),
    )


def build_demo_bundle(bundle_dir: Path, *, source_sha: str) -> Path:
    source_sha = validate_demo_source_sha(source_sha)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    projections = tuple(build_demo_projection(company) for company in DEMO_COHORT)
    companies_path = bundle_dir / "companies.jsonl.gz"
    with companies_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            for projection in projections:
                zipped.write(canonical_json(projection.model_dump(mode="json")) + b"\n")
    cohort_hash = hashlib.sha256(
        canonical_json([{"inn": item.inn, "role": item.role} for item in DEMO_COHORT])
    ).hexdigest()
    manifest = ReleaseManifest(
        schema_version="public-projection-v1",
        release_id=DEMO_RELEASE_ID,
        source_main_sha=source_sha,
        cohort_manifest_path="docs/releases/nextcompany-demo-v1.json",
        cohort_manifest_sha256=cohort_hash,
        cohort_source_main_sha=source_sha,
        previous_release_id=None,
        created_at=DEMO_TIMESTAMP,
        result_date=DEMO_RESULT_DATE,
        content_updated_at=DEMO_TIMESTAMP,
        record_count=len(projections),
        companies_file="companies.jsonl.gz",
    )
    (bundle_dir / "manifest.json").write_bytes(
        canonical_json(manifest.model_dump(mode="json")) + b"\n"
    )
    write_checksums(bundle_dir)
    return bundle_dir


def _semantic_evidence(company: DemoCompany, value: str) -> dict[str, Any]:
    return {
        "evidence_ref": f"demo:{_stable_uuid('evidence', company.inn)}",
        "source_code": "NEXTCOMPANY_DEMO",
        "source_name": DEMO_SOURCE_NAME,
        "source_class": DEMO_SOURCE_CLASS,
        "source_data_date": DEMO_RESULT_DATE.isoformat(),
        "value": value,
    }


def seed_demo_companies(session: Session) -> dict[str, Company]:
    result: dict[str, Company] = {}
    for index, demo in enumerate(DEMO_COHORT, start=1):
        company = session.scalar(sa.select(Company).where(Company.inn == demo.inn))
        if company is None:
            company = Company(inn=demo.inn, name=demo.name, entity_type="legal")
            session.add(company)
            session.flush()
        elif company.master_source not in {None, "NEXTCOMPANY_DEMO"} or (
            "СИНТЕТИЧЕСКАЯ ДЕМО" not in company.name
        ):
            raise RuntimeError(f"refusing to replace non-Demo company with INN {demo.inn}")
        company.name = demo.name
        company.short_name = demo.name
        company.full_name = demo.name
        company.entity_type = "legal"
        company.kpp = f"900{index:03d}001"
        company.ogrn = f"1269000000{index:03d}"
        company.status = demo.status
        company.registration_date = date(2024, 1, 15)
        company.address = "Демо-среда, синтетический адрес"
        company.master_authority = "DEMO_SYNTHETIC"
        company.master_source = "NEXTCOMPANY_DEMO"
        company.official_registry_verified = False
        company.master_provenance = {
            "source_name": DEMO_SOURCE_NAME,
            "source_class": DEMO_SOURCE_CLASS,
            "synthetic": True,
        }
        company.source = "NEXTCOMPANY_DEMO"
        session.flush()
        fact_ref = f"fact:{_stable_uuid('semantic-fact', demo.inn)}"
        fact = session.get(CompanySemanticFact, fact_ref)
        if fact is None:
            fact = CompanySemanticFact(
                fact_ref=fact_ref,
                item_ref=f"item:{_stable_uuid('semantic-item', demo.inn)}",
                company_id=company.id,
                section_key="demo_profile",
                field_key="demo_indicator",
                period_identity="DATE:2026-01-15",
                item_identity=demo.role,
                selected_evidence=_semantic_evidence(demo, demo.indicator),
                alternative_evidence=[],
                evidence_history=[],
                state="FOUND",
                rights="PUBLIC",
                is_current=True,
            )
            session.add(fact)
        elif fact.company_id != company.id:
            raise RuntimeError(f"Demo semantic fact collision for INN {demo.inn}")
        result[demo.inn] = company
    session.flush()
    return result


def _reconcile_demo_entitlements(session: Session, workspace_id: UUID) -> None:
    settings: dict[str, tuple[bool, int | None]] = {
        "workspace.core.enabled": (True, None),
        "saved_companies.enabled": (True, 20),
        "monitoring.enabled": (True, None),
        "reports.enabled": (True, 50),
        "bulk_check.enabled": (True, 100),
        "workspace_members.enabled": (True, 10),
    }
    rows = {
        item.entitlement_key: item
        for item in session.scalars(
            sa.select(WorkspaceEntitlement).where(
                WorkspaceEntitlement.workspace_id == workspace_id
            )
        ).all()
    }
    for key, (enabled, limit) in settings.items():
        item = rows.get(key)
        if item is None:
            raise RuntimeError(f"bootstrap did not create required entitlement {key}")
        item.enabled = enabled
        item.limit_value = limit
        item.policy_version = DEMO_POLICY_VERSION
    session.flush()


def bootstrap_demo_account(session: Session, *, password: str) -> tuple[CustomerUser, Workspace, bool]:
    user = session.scalar(sa.select(CustomerUser).where(CustomerUser.email == DEMO_OWNER_EMAIL))
    created = user is None
    if user is None:
        user, workspace = bootstrap_workspace_owner(
            session,
            email=DEMO_OWNER_EMAIL,
            password_hash=hash_password(password),
            workspace_name=DEMO_WORKSPACE_NAME,
            saved_company_limit=20,
        )
    else:
        if user.status != "active" or not verify_password(password, user.password_hash):
            raise RuntimeError(
                "existing Demo owner does not match NEXTCOMPANY_DEMO_PASSWORD; reset the Demo databases"
            )
        memberships = session.execute(
            sa.select(WorkspaceMembership, Workspace, WorkspaceRole)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .join(WorkspaceRole, WorkspaceRole.id == WorkspaceMembership.role_id)
            .where(
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.status == "active",
            )
        ).all()
        matching = [row for row in memberships if row[1].name == DEMO_WORKSPACE_NAME]
        if len(matching) != 1 or matching[0][2].role_key != "OWNER":
            raise RuntimeError("existing Demo owner/workspace state is not reconcilable; reset required")
        workspace = matching[0][1]
    _reconcile_demo_entitlements(session, workspace.id)
    return user, workspace, created


def _demo_fact(session: Session, company_id: int) -> CompanySemanticFact:
    fact = session.scalar(
        sa.select(CompanySemanticFact).where(
            CompanySemanticFact.company_id == company_id,
            CompanySemanticFact.section_key == "demo_profile",
            CompanySemanticFact.field_key == "demo_indicator",
            CompanySemanticFact.is_current.is_(True),
        )
    )
    if fact is None:
        raise RuntimeError("Demo monitoring fact is missing")
    return fact


def advance_demo_event(session: Session, *, inn: str = "9000000046") -> dict[str, Any]:
    demo = next((item for item in DEMO_COHORT if item.inn == inn), None)
    if demo is None:
        raise ValueError("INN is outside the Demo cohort")
    company = session.scalar(sa.select(Company).where(Company.inn == demo.inn))
    if company is None or company.master_source != "NEXTCOMPANY_DEMO":
        raise RuntimeError("Demo company is not bootstrapped")
    fact = _demo_fact(session, company.id)
    evidence = dict(fact.selected_evidence or {})
    already_advanced = evidence.get("value") == demo.advanced_indicator
    if not already_advanced:
        history = list(fact.evidence_history or [])
        history.append(dict(evidence))
        fact.evidence_history = history
        fact.selected_evidence = _semantic_evidence(demo, demo.advanced_indicator)
        fact.updated_at = datetime.now(UTC)
        session.flush()
    result = monitor_company_once(session, company_id=company.id)
    return {
        "inn": demo.inn,
        "advanced": not already_advanced,
        "detected_changes": result.detected_change_count,
        "events": result.canonical_event_count,
        "feed_entries": result.feed_entry_count,
    }


def _ensure_showcase_member(
    session: Session, *, owner: CustomerUser, workspace: Workspace, password: str
) -> None:
    existing = session.scalar(
        sa.select(WorkspaceMembership.id)
        .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.workspace_id == workspace.id,
            CustomerUser.email == DEMO_MEMBER_EMAIL,
            WorkspaceMembership.status == "active",
        )
    )
    if existing is not None:
        return
    role_id = session.scalar(
        sa.select(WorkspaceRole.id).where(
            WorkspaceRole.workspace_id == workspace.id,
            WorkspaceRole.role_key == "MEMBER",
        )
    )
    if role_id is None:
        raise RuntimeError("Demo MEMBER role is missing")
    pending = session.scalar(
        sa.select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace.id,
            WorkspaceInvitation.email == DEMO_MEMBER_EMAIL,
            WorkspaceInvitation.status == "PENDING",
        )
    )
    if pending is not None:
        raise RuntimeError("stale Demo invitation exists; reset required")
    secret = create_invitation(
        session,
        user_id=owner.id,
        workspace_id=workspace.id,
        email=DEMO_MEMBER_EMAIL,
        role_id=role_id,
    )
    accept_invitation(
        session,
        token=secret.token,
        password=password,
        password_confirmation=password,
    )


def create_showcase_state(
    session: Session,
    *,
    owner: CustomerUser,
    workspace: Workspace,
    password: str,
    public_repository: PublicRepository,
) -> None:
    stable = DEMO_COHORT[0]
    monitoring = DEMO_COHORT[3]
    for demo in (stable, monitoring):
        save_company(
            session,
            user_id=owner.id,
            workspace_id=workspace.id,
            inn=demo.inn,
        )
    saved = saved_company_for_inn(
        session,
        user_id=owner.id,
        workspace_id=workspace.id,
        inn=stable.inn,
    )
    if saved is not None:
        update_saved_company_note(
            session,
            user_id=owner.id,
            workspace_id=workspace.id,
            saved_company_id=saved.saved_company_id,
            note="Синтетическая заметка для демонстрации сохранённой компании.",
        )
    subscribe_company(
        session,
        user_id=owner.id,
        workspace_id=workspace.id,
        inn=monitoring.inn,
    )
    advance_demo_event(session, inn=monitoring.inn)
    if not session.scalar(
        sa.select(WorkspaceReport.id).where(WorkspaceReport.workspace_id == workspace.id)
    ):
        generate_report(
            session,
            user_id=owner.id,
            workspace_id=workspace.id,
            inn=DEMO_COHORT[1].inn,
            projection_repository=public_repository,
        )
    if not session.scalar(
        sa.select(WorkspaceBulkJob.id).where(WorkspaceBulkJob.workspace_id == workspace.id)
    ):
        content = (
            "inn\n"
            f"{DEMO_COHORT[0].inn}\n"
            f"{DEMO_COHORT[4].inn}\n"
            f"{DEMO_COHORT[0].inn}\n"
            "123\n"
        ).encode("utf-8")
        job = create_bulk_job(
            session,
            user_id=owner.id,
            workspace_id=workspace.id,
            filename="nextcompany-demo.csv",
            content=content,
        )
        while job.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED"}:
            job = process_bulk_job_chunk(
                session,
                user_id=owner.id,
                workspace_id=workspace.id,
                job_id=job.id,
                projection_repository=public_repository,
            )
    _ensure_showcase_member(
        session,
        owner=owner,
        workspace=workspace,
        password=password,
    )
    session.flush()


def _workspace_counts(session: Session, workspace_id: UUID) -> dict[str, int]:
    models: tuple[tuple[str, Any], ...] = (
        ("members", WorkspaceMembership),
        ("invitations", WorkspaceInvitation),
        ("saved", SavedCompany),
        ("monitoring", MonitoringSubscription),
        ("feed", WorkspaceFeedEntry),
        ("reports", WorkspaceReport),
        ("bulk", WorkspaceBulkJob),
    )
    result: dict[str, int] = {}
    for key, model in models:
        result[key] = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(model).where(
                    model.workspace_id == workspace_id
                )
            )
            or 0
        )
    return result


def _public_release_state(public_import_url: str) -> dict[str, Any]:
    with psycopg.connect(_psycopg_url(public_import_url), row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT r.release_id, r.record_count, r.source_main_sha, r.manifest_sha256,
                   (SELECT count(*) FROM public_company_projections p
                    WHERE p.release_id=r.release_id) AS projection_count
            FROM public_publication_state s
            LEFT JOIN public_releases r ON r.release_id=s.active_release_id
            WHERE s.singleton=TRUE
            """
        ).fetchone()
    return dict(row or {})


def bootstrap_demo(
    *,
    operational_url: str,
    public_import_url: str,
    public_web_url: str,
    password: str,
    profile: str,
) -> dict[str, Any]:
    if profile not in {"clean", "showcase"}:
        raise ValueError("profile must be clean or showcase")
    validate_demo_topology(
        operational_url=operational_url,
        public_import_url=public_import_url,
        public_web_url=public_web_url,
    )
    verify_migration_head(
        operational_url, expected=EXPECTED_OPERATIONAL_HEAD, label="operational"
    )
    verify_migration_head(public_import_url, expected=EXPECTED_PUBLIC_HEAD, label="public")
    source_sha = resolve_demo_source_sha()
    with tempfile.TemporaryDirectory(prefix="nextcompany-demo-bundle-") as directory:
        bundle = build_demo_bundle(Path(directory), source_sha=source_sha)
        with psycopg.connect(_psycopg_url(public_import_url)) as connection:
            existing = connection.execute(
                "SELECT source_main_sha FROM public_releases WHERE release_id=%s",
                (DEMO_RELEASE_ID,),
            ).fetchone()
            if existing is not None and str(existing[0]) != source_sha:
                raise RuntimeError(
                    "existing Demo release belongs to a different source SHA; Demo reset required"
                )
            try:
                release_result = import_release(connection, bundle, DEMO_RELEASE_ID)
            except ValueError as exc:
                if "release_id already exists with different content" in str(exc):
                    raise RuntimeError(
                        "existing Demo release content differs from exact HEAD; Demo reset required"
                    ) from exc
                raise
            imported_source_sha = connection.execute(
                "SELECT source_main_sha FROM public_releases WHERE release_id=%s",
                (DEMO_RELEASE_ID,),
            ).fetchone()
            if imported_source_sha is None or str(imported_source_sha[0]) != source_sha:
                raise RuntimeError("imported Demo release source SHA does not match exact HEAD")

    engine = sa.create_engine(operational_url, pool_pre_ping=True)
    DemoSession = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with DemoSession() as session:
            seed_demo_companies(session)
            owner, workspace, created = bootstrap_demo_account(session, password=password)
            if profile == "showcase":
                create_showcase_state(
                    session,
                    owner=owner,
                    workspace=workspace,
                    password=password,
                    public_repository=PublicRepository(public_web_url),
                )
            session.commit()
            counts = _workspace_counts(session, workspace.id)
            if profile == "clean" and any(
                counts[key]
                for key in ("invitations", "saved", "monitoring", "feed", "reports", "bulk")
            ):
                raise RuntimeError("CLEAN profile requires reset because product state already exists")
            status = "created" if created else "already_ready"
            return {
                "status": status,
                "profile": profile.upper(),
                "owner_email": DEMO_OWNER_EMAIL,
                "workspace": DEMO_WORKSPACE_NAME,
                "workspace_id": str(workspace.id),
                "release_id": str(release_result["release_id"]),
                "source_main_sha": source_sha,
                "cohort_size": len(DEMO_COHORT),
                "company_inns": [item.inn for item in DEMO_COHORT],
                "counts": counts,
                "operational_migration_head": EXPECTED_OPERATIONAL_HEAD,
                "public_migration_head": EXPECTED_PUBLIC_HEAD,
            }
    finally:
        engine.dispose()


def reset_demo(
    *,
    operational_url: str,
    public_import_url: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != RESET_CONFIRMATION:
        raise ValueError(f"reset requires --confirm {RESET_CONFIRMATION}")
    validate_demo_topology(
        operational_url=operational_url,
        public_import_url=public_import_url,
    )
    with psycopg.connect(_psycopg_url(public_import_url), row_factory=dict_row) as connection:
        releases = connection.execute(
            "SELECT release_id FROM public_releases ORDER BY release_id"
        ).fetchall()
        unknown = [
            str(row["release_id"])
            for row in releases
            if not str(row["release_id"]).startswith(DEMO_RELEASE_PREFIX)
        ]
        if unknown:
            raise RuntimeError("refusing reset: public Demo database contains an unknown release")
        connection.execute("DELETE FROM public_publication_state")
        connection.execute("DELETE FROM public_company_projections")
        connection.execute("DELETE FROM public_releases")
        connection.execute(
            "INSERT INTO public_publication_state(singleton,active_release_id) VALUES(TRUE,NULL)"
        )

    engine = sa.create_engine(operational_url, pool_pre_ping=True)
    DemoSession = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with DemoSession() as session:
            workspace_ids = tuple(
                session.scalars(
                    sa.select(Workspace.id)
                    .outerjoin(
                        WorkspaceMembership,
                        WorkspaceMembership.workspace_id == Workspace.id,
                    )
                    .outerjoin(
                        CustomerUser,
                        CustomerUser.id == WorkspaceMembership.user_id,
                    )
                    .where(
                        sa.or_(
                            Workspace.name == DEMO_WORKSPACE_NAME,
                            CustomerUser.email == DEMO_OWNER_EMAIL,
                        )
                    )
                    .distinct()
                ).all()
            )
            if workspace_ids:
                session.execute(sa.delete(Workspace).where(Workspace.id.in_(workspace_ids)))
                session.flush()
            session.execute(
                sa.delete(CustomerUser).where(
                    CustomerUser.email.in_((DEMO_OWNER_EMAIL, DEMO_MEMBER_EMAIL))
                )
            )
            session.execute(
                sa.delete(Company).where(
                    Company.inn.in_(tuple(item.inn for item in DEMO_COHORT))
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return {"status": "reset", "production_mutation": False}


def demo_acceptance_truth(
    *,
    operational_url: str,
    public_import_url: str,
    public_web_url: str,
) -> dict[str, Any]:
    validate_demo_topology(
        operational_url=operational_url,
        public_import_url=public_import_url,
        public_web_url=public_web_url,
    )
    repository = PublicRepository(public_web_url)
    ready, release_id, count = repository.ready()
    release = _public_release_state(public_import_url)
    engine = sa.create_engine(operational_url, pool_pre_ping=True)
    DemoSession = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with DemoSession() as session:
            workspace = session.scalar(
                sa.select(Workspace)
                .join(
                    WorkspaceMembership,
                    WorkspaceMembership.workspace_id == Workspace.id,
                )
                .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
                .where(CustomerUser.email == DEMO_OWNER_EMAIL)
                .order_by(WorkspaceMembership.id)
                .limit(1)
            )
            counts = _workspace_counts(session, workspace.id) if workspace else {}
            event_count = int(
                session.scalar(sa.select(sa.func.count()).select_from(MonitoringEvent)) or 0
            )
    finally:
        engine.dispose()
    with psycopg.connect(_psycopg_url(public_import_url)) as connection:
        eligible = int(
            connection.execute(
                "SELECT count(*) FROM public_company_projections WHERE index_eligible=TRUE"
            ).fetchone()[0]
        )
    return {
        "public_repository_ready": ready,
        "release_id": release_id,
        "repository_count": count,
        "release_record_count": int(release.get("record_count") or 0),
        "projection_count": int(release.get("projection_count") or 0),
        "public_release_source_main_sha": release.get("source_main_sha"),
        "public_release_manifest_sha256": release.get("manifest_sha256"),
        "index_eligible_count": eligible,
        "workspace_counts": counts,
        "monitoring_events": event_count,
        "production_mutation": False,
    }


def redacted_summary(value: dict[str, Any]) -> str:
    """Serialize the allow-listed CLI result; secrets are never accepted here."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
