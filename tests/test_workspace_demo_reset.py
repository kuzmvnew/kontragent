from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import sqlalchemy as sa
from psycopg.rows import dict_row
from sqlalchemy.orm import Session

from app.database.postgres import engine
from app.models.company import Company
from app.models.semantic_fact import CompanySemanticFact
from app.models.workspace import (
    CustomerSession,
    CustomerUser,
    Workspace,
    WorkspaceEntitlement,
    WorkspaceMembership,
    WorkspaceRole,
)
from scripts.import_public_release import import_release
from scripts.workspace_demo_support import (
    DEMO_COHORT,
    DEMO_ENTITLEMENT_KEYS,
    DEMO_EXPECTED_SHA_ENV,
    DEMO_MEMBER_EMAIL,
    DEMO_OWNER_EMAIL,
    DEMO_POLICY_VERSION,
    DEMO_RELEASE_ID,
    DEMO_WORKSPACE_NAME,
    RESET_CONFIRMATION,
    bootstrap_demo,
    build_demo_bundle,
    reset_demo,
)
from workspace_app.auth import hash_password
from workspace_app.service import bootstrap_workspace_owner


RUN_E2E = os.getenv("WORKSPACE_DEMO_E2E") == "1"
OPERATIONAL_URL = os.getenv("DATABASE_URL", "")
PUBLIC_IMPORT_URL = os.getenv("PUBLIC_IMPORT_DATABASE_URL", "")
PUBLIC_WEB_URL = os.getenv("PUBLIC_DATABASE_URL", "")
DEMO_PASSWORD = os.getenv("NEXTCOMPANY_DEMO_PASSWORD", "")
EXPECTED_SOURCE_SHA = os.getenv(DEMO_EXPECTED_SHA_ENV, "")
pytestmark = pytest.mark.skipif(
    not (
        RUN_E2E
        and OPERATIONAL_URL
        and PUBLIC_IMPORT_URL
        and PUBLIC_WEB_URL
        and DEMO_PASSWORD
        and EXPECTED_SOURCE_SHA
    ),
    reason="Workspace Demo reset PostgreSQL environment is not configured",
)


def _import_url() -> str:
    return PUBLIC_IMPORT_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _reset() -> dict:
    return reset_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        confirmation=RESET_CONFIRMATION,
    )


def _bootstrap(profile: str = "clean") -> dict:
    return bootstrap_demo(
        operational_url=OPERATIONAL_URL,
        public_import_url=PUBLIC_IMPORT_URL,
        public_web_url=PUBLIC_WEB_URL,
        password=DEMO_PASSWORD,
        profile=profile,
    )


def _public_snapshot() -> dict:
    with psycopg.connect(_import_url(), row_factory=dict_row) as connection:
        state = connection.execute(
            "SELECT active_release_id, updated_at FROM public_publication_state "
            "WHERE singleton=TRUE"
        ).fetchone()
        releases = connection.execute(
            "SELECT row_to_json(item)::text AS row FROM "
            "(SELECT * FROM public_releases ORDER BY release_id) item"
        ).fetchall()
        projections = connection.execute(
            "SELECT release_id, inn, payload_sha256 FROM public_company_projections "
            "ORDER BY release_id, inn"
        ).fetchall()
        return {
            "active_release_id": state["active_release_id"],
            "publication_updated_at": state["updated_at"],
            "releases": tuple(row["row"] for row in releases),
            "projections": tuple(
                (row["release_id"], row["inn"], row["payload_sha256"])
                for row in projections
            ),
        }


def _operational_counts(session: Session) -> tuple[int, ...]:
    return tuple(
        int(session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)
        for model in (
            Company,
            CompanySemanticFact,
            Workspace,
            CustomerUser,
            WorkspaceMembership,
            WorkspaceEntitlement,
            CustomerSession,
        )
    )


def _create_unrelated_workspace(
    session: Session, *, name: str = "Unrelated Workspace"
) -> tuple[CustomerUser, Workspace]:
    return bootstrap_workspace_owner(
        session,
        email=f"unrelated-{uuid4()}@example.test",
        password_hash=hash_password("Unrelated-Workspace-Password-2026"),
        workspace_name=name,
        saved_company_limit=20,
    )


def _add_membership(
    session: Session, *, user_id: UUID, workspace_id: UUID, role_key: str = "MEMBER"
) -> None:
    role_id = session.scalar(
        sa.select(WorkspaceRole.id).where(
            WorkspaceRole.workspace_id == workspace_id,
            WorkspaceRole.role_key == role_key,
        )
    )
    assert role_id is not None
    session.add(
        WorkspaceMembership(
            id=uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            role_id=role_id,
            status="active",
        )
    )


def _delete_unrelated(*, workspace_id: UUID, user_id: UUID) -> None:
    with Session(engine) as session:
        session.execute(sa.delete(Workspace).where(Workspace.id == workspace_id))
        session.flush()
        session.execute(sa.delete(CustomerUser).where(CustomerUser.id == user_id))
        session.commit()


def _import_public_demo_release() -> None:
    with tempfile.TemporaryDirectory(prefix="nextcompany-reset-public-") as directory:
        bundle = build_demo_bundle(Path(directory), source_sha=EXPECTED_SOURCE_SHA)
        with psycopg.connect(_import_url()) as connection:
            import_release(connection, bundle, DEMO_RELEASE_ID)


def test_non_demo_company_collision_blocks_reset_without_any_mutation():
    _reset()
    _import_public_demo_release()
    collision_id: int | None = None
    try:
        with Session(engine) as session:
            collision = Company(
                inn=DEMO_COHORT[0].inn,
                name="Unrelated company with colliding INN",
                entity_type="legal",
                master_source="OTHER",
                master_authority="OFFICIAL",
                master_provenance={
                    "synthetic": False,
                    "source_class": "OFFICIAL",
                    "source_name": "Unrelated source",
                },
                source="OTHER",
            )
            session.add(collision)
            session.commit()
            collision_id = int(collision.id)

        before_public = _public_snapshot()
        with Session(engine) as session:
            before_counts = _operational_counts(session)
            before_company = session.get(Company, collision_id)
            assert before_company is not None
            before_fields = (
                before_company.name,
                before_company.master_source,
                before_company.master_authority,
                dict(before_company.master_provenance or {}),
                before_company.source,
            )

        with pytest.raises(RuntimeError, match="belongs to non-Demo company"):
            _reset()

        assert _public_snapshot() == before_public
        with Session(engine) as session:
            assert _operational_counts(session) == before_counts
            after_company = session.get(Company, collision_id)
            assert after_company is not None
            assert (
                after_company.name,
                after_company.master_source,
                after_company.master_authority,
                dict(after_company.master_provenance or {}),
                after_company.source,
            ) == before_fields
    finally:
        if collision_id is not None:
            with Session(engine) as session:
                session.execute(sa.delete(Company).where(Company.id == collision_id))
                session.commit()
        _reset()


def test_workspace_name_and_partial_marker_collisions_fail_before_public_reset():
    _reset()
    _bootstrap("clean")
    with Session(engine) as session:
        unrelated_user, unrelated_workspace = _create_unrelated_workspace(
            session, name=DEMO_WORKSPACE_NAME
        )
        session.commit()
        unrelated_user_id = unrelated_user.id
        unrelated_workspace_id = unrelated_workspace.id
    try:
        before_public = _public_snapshot()
        with pytest.raises(RuntimeError, match="unproven tenant"):
            _reset()
        assert _public_snapshot() == before_public
        with Session(engine) as session:
            assert session.get(Workspace, unrelated_workspace_id) is not None
            assert session.get(CustomerUser, unrelated_user_id) is not None
    finally:
        _delete_unrelated(
            workspace_id=unrelated_workspace_id, user_id=unrelated_user_id
        )
        _reset()

    clean = _bootstrap("clean")
    demo_workspace_id = UUID(clean["workspace_id"])
    with Session(engine) as session:
        owner_membership = session.scalar(
            sa.select(WorkspaceMembership)
            .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == demo_workspace_id,
                CustomerUser.email == DEMO_OWNER_EMAIL,
            )
        )
        member_role_id = session.scalar(
            sa.select(WorkspaceRole.id).where(
                WorkspaceRole.workspace_id == demo_workspace_id,
                WorkspaceRole.role_key == "MEMBER",
            )
        )
        assert owner_membership is not None and member_role_id is not None
        owner_role_id = owner_membership.role_id
        owner_membership.role_id = member_role_id
        session.commit()
    try:
        before_public = _public_snapshot()
        with pytest.raises(RuntimeError, match="lacks an active Demo OWNER"):
            _reset()
        assert _public_snapshot() == before_public
    finally:
        with Session(engine) as session:
            membership = session.scalar(
                sa.select(WorkspaceMembership)
                .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
                .where(
                    WorkspaceMembership.workspace_id == demo_workspace_id,
                    CustomerUser.email == DEMO_OWNER_EMAIL,
                )
            )
            assert membership is not None
            membership.role_id = owner_role_id
            session.commit()
        _reset()

    _bootstrap("clean")
    with Session(engine) as session:
        unrelated_user, unrelated_workspace = _create_unrelated_workspace(session)
        marker_rows = session.scalars(
            sa.select(WorkspaceEntitlement)
            .where(
                WorkspaceEntitlement.workspace_id == unrelated_workspace.id,
                WorkspaceEntitlement.entitlement_key.in_(DEMO_ENTITLEMENT_KEYS[:2]),
            )
            .order_by(WorkspaceEntitlement.entitlement_key)
        ).all()
        assert len(marker_rows) == 2
        for row in marker_rows:
            row.policy_version = DEMO_POLICY_VERSION
        session.commit()
        unrelated_user_id = unrelated_user.id
        unrelated_workspace_id = unrelated_workspace.id
    try:
        before_public = _public_snapshot()
        with pytest.raises(RuntimeError, match="partial Demo entitlement markers"):
            _reset()
        assert _public_snapshot() == before_public
        with Session(engine) as session:
            assert session.get(Workspace, unrelated_workspace_id) is not None
    finally:
        _delete_unrelated(
            workspace_id=unrelated_workspace_id, user_id=unrelated_user_id
        )
        _reset()


@pytest.mark.parametrize(
    ("profile", "demo_email"),
    (("clean", DEMO_OWNER_EMAIL), ("showcase", DEMO_MEMBER_EMAIL)),
)
def test_demo_global_user_with_other_workspace_membership_blocks_reset(
    profile: str, demo_email: str
):
    _reset()
    _bootstrap(profile)
    with Session(engine) as session:
        demo_user = session.scalar(
            sa.select(CustomerUser).where(CustomerUser.email == demo_email)
        )
        assert demo_user is not None
        unrelated_user, unrelated_workspace = _create_unrelated_workspace(session)
        _add_membership(
            session, user_id=demo_user.id, workspace_id=unrelated_workspace.id
        )
        session.commit()
        unrelated_user_id = unrelated_user.id
        unrelated_workspace_id = unrelated_workspace.id
        demo_user_id = demo_user.id
    try:
        before_public = _public_snapshot()
        with pytest.raises(RuntimeError, match="unproven workspace ownership"):
            _reset()
        assert _public_snapshot() == before_public
        with Session(engine) as session:
            assert session.get(CustomerUser, demo_user_id) is not None
            assert session.get(Workspace, unrelated_workspace_id) is not None
    finally:
        _delete_unrelated(
            workspace_id=unrelated_workspace_id, user_id=unrelated_user_id
        )
        _reset()


def test_demo_session_outside_proven_workspace_blocks_reset():
    _reset()
    _bootstrap("clean")
    with Session(engine) as session:
        owner = session.scalar(
            sa.select(CustomerUser).where(CustomerUser.email == DEMO_OWNER_EMAIL)
        )
        assert owner is not None
        unrelated_user, unrelated_workspace = _create_unrelated_workspace(session)
        session.add(
            CustomerSession(
                token_hash=uuid4().hex + uuid4().hex,
                csrf_hash=uuid4().hex + uuid4().hex,
                user_id=owner.id,
                active_workspace_id=unrelated_workspace.id,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.commit()
        unrelated_user_id = unrelated_user.id
        unrelated_workspace_id = unrelated_workspace.id
    try:
        before_public = _public_snapshot()
        with pytest.raises(RuntimeError, match="session outside the Demo workspace"):
            _reset()
        assert _public_snapshot() == before_public
    finally:
        _delete_unrelated(
            workspace_id=unrelated_workspace_id, user_id=unrelated_user_id
        )
        _reset()


def test_genuine_demo_exact_plan_idempotency_clean_and_showcase_lifecycle():
    _reset()
    showcase = _bootstrap("showcase")
    demo_workspace_id = UUID(showcase["workspace_id"])
    control_company_id: int | None = None
    control_workspace_id: UUID | None = None
    control_user_id: UUID | None = None
    try:
        with Session(engine) as session:
            control_user, control_workspace = _create_unrelated_workspace(
                session, name="Reset control tenant"
            )
            control_company = Company(
                inn="7712345678",
                name="Reset control company",
                entity_type="legal",
                master_source="OTHER",
                master_authority="OFFICIAL",
                master_provenance={"synthetic": False},
                source="OTHER",
            )
            session.add(control_company)
            session.commit()
            control_company_id = int(control_company.id)
            control_workspace_id = control_workspace.id
            control_user_id = control_user.id

        result = _reset()
        assert result["status"] == "reset"
        assert result["deleted"] == {
            "public_releases": 1,
            "workspaces": 1,
            "users": 2,
            "companies": len(DEMO_COHORT),
        }
        with Session(engine) as session:
            assert session.get(Workspace, demo_workspace_id) is None
            assert session.get(Workspace, control_workspace_id) is not None
            assert session.get(CustomerUser, control_user_id) is not None
            assert session.get(Company, control_company_id) is not None
            assert not session.scalar(
                sa.select(CustomerUser.id).where(
                    CustomerUser.email.in_((DEMO_OWNER_EMAIL, DEMO_MEMBER_EMAIL))
                )
            )
            assert not session.scalar(
                sa.select(Company.id).where(
                    Company.inn.in_(tuple(item.inn for item in DEMO_COHORT))
                )
            )
        assert not _public_snapshot()["releases"]
        assert not _public_snapshot()["projections"]

        second = _reset()
        assert second["status"] == "already_reset"
        assert all(value == 0 for value in second["deleted"].values())

        clean = _bootstrap("clean")
        assert clean["source_main_sha"] == EXPECTED_SOURCE_SHA
        assert clean["counts"] == {
            "members": 1,
            "invitations": 0,
            "saved": 0,
            "monitoring": 0,
            "feed": 0,
            "reports": 0,
            "bulk": 0,
        }
        _reset()
        rebuilt_showcase = _bootstrap("showcase")
        assert rebuilt_showcase["source_main_sha"] == EXPECTED_SOURCE_SHA
        assert rebuilt_showcase["counts"]["members"] == 2
        assert rebuilt_showcase["counts"]["saved"] >= 2
        assert rebuilt_showcase["counts"]["monitoring"] == 1
        assert rebuilt_showcase["counts"]["reports"] == 1
        assert rebuilt_showcase["counts"]["bulk"] == 1
    finally:
        _reset()
        if control_workspace_id is not None and control_user_id is not None:
            _delete_unrelated(
                workspace_id=control_workspace_id, user_id=control_user_id
            )
        if control_company_id is not None:
            with Session(engine) as session:
                session.execute(
                    sa.delete(Company).where(Company.id == control_company_id)
                )
                session.commit()
