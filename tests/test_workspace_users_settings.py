from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.workspace import (
    CustomerSession,
    CustomerUser,
    SavedCompany,
    Workspace,
    WorkspaceAuditEvent,
    WorkspaceBulkJob,
    WorkspaceEntitlement,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceReport,
    WorkspaceRole,
    WorkspaceRoleCapability,
)
from workspace_app.auth import create_customer_session, hash_password, verify_password
from workspace_app.main import create_app
from workspace_app.member_service import (
    accept_invitation,
    change_member_role,
    change_member_status,
    create_invitation,
    invitation_preview,
    reissue_invitation,
    revoke_invitation,
)
from workspace_app.service import (
    ActionDenied,
    PERMISSION_ENTITLEMENTS,
    bootstrap_workspace_owner,
)
from workspace_app.settings_service import get_workspace_settings, update_workspace_name


PASSWORD = "correct-horse-battery-staple"


class EmptyPublicRepository:
    def get_company(self, _inn):
        return None

    def search(self, _query, limit=20):
        return []


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, html
    return match.group(1)


def _bootstrap(email: str, name: str, *, member_limit: int | None = None):
    with Session(engine) as session:
        user, workspace = bootstrap_workspace_owner(
            session,
            email=email,
            password_hash=hash_password(PASSWORD),
            workspace_name=name,
            saved_company_limit=10,
        )
        if member_limit is not None:
            entitlement = session.scalar(
                sa.select(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id == workspace.id,
                    WorkspaceEntitlement.entitlement_key == "workspace_members.enabled",
                )
            )
            entitlement.limit_value = member_limit
        user_id, workspace_id = user.id, workspace.id
        session.commit()
    return user_id, workspace_id


def _role_id(workspace_id: UUID, role_key: str) -> UUID:
    with Session(engine) as session:
        value = session.scalar(
            sa.select(WorkspaceRole.id).where(
                WorkspaceRole.workspace_id == workspace_id,
                WorkspaceRole.role_key == role_key,
            )
        )
        assert value is not None
        return value


def _add_user_membership(
    workspace_id: UUID,
    email: str,
    *,
    role_key: str = "MEMBER",
    status: str = "active",
):
    with Session(engine) as session:
        user = CustomerUser(
            id=uuid4(),
            email=email,
            password_hash=hash_password(PASSWORD),
            status="active",
        )
        membership = WorkspaceMembership(
            id=uuid4(),
            workspace_id=workspace_id,
            user_id=user.id,
            role_id=_role_id(workspace_id, role_key),
            status=status,
        )
        session.add_all((user, membership))
        session.commit()
        return user.id, membership.id


def _grant_existing_membership(
    user_id: UUID,
    workspace_id: UUID,
    *,
    role_key: str = "MEMBER",
) -> UUID:
    with Session(engine) as session:
        membership = WorkspaceMembership(
            id=uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            role_id=_role_id(workspace_id, role_key),
            status="active",
        )
        session.add(membership)
        session.commit()
        return membership.id


def _purge(workspace_ids: tuple[UUID, ...], emails: tuple[str, ...]) -> None:
    with Session(engine) as session:
        user_ids = tuple(
            session.scalars(sa.select(CustomerUser.id).where(CustomerUser.email.in_(emails))).all()
        )
        if workspace_ids:
            session.execute(
                sa.delete(WorkspaceAuditEvent).where(
                    WorkspaceAuditEvent.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(CustomerSession).where(
                    sa.or_(
                        CustomerSession.active_workspace_id.in_(workspace_ids),
                        CustomerSession.user_id.in_(user_ids) if user_ids else sa.false(),
                    )
                )
            )
            session.execute(
                sa.delete(WorkspaceBulkJob).where(WorkspaceBulkJob.workspace_id.in_(workspace_ids))
            )
            session.execute(
                sa.delete(WorkspaceReport).where(WorkspaceReport.workspace_id.in_(workspace_ids))
            )
            session.execute(
                sa.delete(SavedCompany).where(SavedCompany.workspace_id.in_(workspace_ids))
            )
            session.execute(
                sa.delete(WorkspaceInvitation).where(
                    WorkspaceInvitation.workspace_id.in_(workspace_ids)
                )
            )
            role_ids = tuple(
                session.scalars(
                    sa.select(WorkspaceRole.id).where(
                        WorkspaceRole.workspace_id.in_(workspace_ids)
                    )
                ).all()
            )
            if role_ids:
                session.execute(
                    sa.delete(WorkspaceRoleCapability).where(
                        WorkspaceRoleCapability.role_id.in_(role_ids)
                    )
                )
            session.execute(
                sa.delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id.in_(workspace_ids)
                )
            )
            session.execute(
                sa.delete(WorkspaceRole).where(WorkspaceRole.workspace_id.in_(workspace_ids))
            )
            session.execute(sa.delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        if user_ids:
            session.execute(
                sa.delete(CustomerSession).where(CustomerSession.user_id.in_(user_ids))
            )
            session.execute(sa.delete(CustomerUser).where(CustomerUser.id.in_(user_ids)))
        session.commit()


def test_fresh_workspace_capabilities_and_entitlement_split():
    owner_email = f"owner-{uuid4()}@example.test"
    user_id, workspace_id = _bootstrap(owner_email, "Fresh capability parity")
    try:
        with Session(engine) as session:
            rows = session.execute(
                sa.select(WorkspaceRole.role_key, WorkspaceRoleCapability.capability_key)
                .join(WorkspaceRoleCapability, WorkspaceRoleCapability.role_id == WorkspaceRole.id)
                .where(WorkspaceRole.workspace_id == workspace_id)
            ).all()
        by_role: dict[str, set[str]] = {}
        for role_key, capability in rows:
            by_role.setdefault(role_key, set()).add(capability)
        for role_key in ("OWNER", "ADMIN"):
            assert {
                "workspace.members.manage",
                "workspace.members.invite",
                "workspace.settings.manage",
            }.issubset(by_role[role_key])
        assert "workspace.members.manage" not in by_role["MEMBER"]
        assert "workspace.members.invite" not in by_role["MEMBER"]
        assert "workspace.settings.manage" not in by_role["MEMBER"]
        assert PERMISSION_ENTITLEMENTS["workspace.members.manage"] == "workspace.core.enabled"
        assert PERMISSION_ENTITLEMENTS["workspace.members.invite"] == "workspace_members.enabled"
        assert PERMISSION_ENTITLEMENTS["workspace.settings.manage"] == "workspace.core.enabled"
    finally:
        _purge((workspace_id,), (owner_email,))


def test_invitation_new_user_token_privacy_and_lifecycle():
    owner_email = f"owner-{uuid4()}@example.test"
    invited_email = f"new-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Invite lifecycle")
    try:
        with Session(engine) as session:
            secret = create_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                email=f"  {invited_email.upper()}  ",
                role_id=_role_id(workspace_id, "ADMIN"),
            )
            raw_token = secret.token
            invitation_id = secret.invitation.invitation_id
            session.commit()

        preview = None
        with Session(engine) as session:
            preview = invitation_preview(session, token=raw_token)
            assert preview.email == invited_email
            assert preview.role_key == "ADMIN"
            row = session.get(WorkspaceInvitation, invitation_id)
            assert row.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
            assert raw_token not in row.token_hash
            audit_refs = session.scalars(
                sa.select(WorkspaceAuditEvent.target_ref).where(
                    WorkspaceAuditEvent.workspace_id == workspace_id
                )
            ).all()
            assert all(raw_token not in value for value in audit_refs)

        with Session(engine) as session:
            accepted = accept_invitation(
                session,
                token=raw_token,
                password=PASSWORD,
                password_confirmation=PASSWORD,
            )
            assert accepted.role_key == "ADMIN"
            session.commit()

        with Session(engine) as session:
            user = session.scalar(sa.select(CustomerUser).where(CustomerUser.email == invited_email))
            assert user is not None and verify_password(PASSWORD, user.password_hash)
            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user.id,
                )
            )
            assert membership is not None and membership.status == "active"
            with pytest.raises(ActionDenied, match="недействительно") as reused:
                accept_invitation(
                    session,
                    token=raw_token,
                    password=PASSWORD,
                    password_confirmation=PASSWORD,
                )
            assert reused.value.code == "invite_invalid_or_expired"
    finally:
        _purge((workspace_id,), (owner_email, invited_email))


def test_existing_user_wrong_password_then_accepts_without_password_change():
    owner_email = f"owner-{uuid4()}@example.test"
    existing_email = f"existing-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Existing account")
    original_hash = hash_password(PASSWORD)
    try:
        with Session(engine) as session:
            session.add(
                CustomerUser(
                    id=uuid4(), email=existing_email, password_hash=original_hash, status="active"
                )
            )
            session.commit()
        with Session(engine) as session:
            secret = create_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                email=existing_email,
                role_id=_role_id(workspace_id, "MEMBER"),
            )
            token = secret.token
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as denied:
                accept_invitation(session, token=token, password="incorrect-password")
            assert denied.value.code == "invite_authentication_failed"
            session.rollback()
        with Session(engine) as session:
            accept_invitation(session, token=token, password=PASSWORD)
            session.commit()
        with Session(engine) as session:
            user = session.scalar(sa.select(CustomerUser).where(CustomerUser.email == existing_email))
            assert user.password_hash == original_hash
    finally:
        _purge((workspace_id,), (owner_email, existing_email))


def test_disabled_customer_cannot_accept_invitation():
    owner_email = f"owner-{uuid4()}@example.test"
    disabled_email = f"disabled-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Disabled acceptance")
    try:
        with Session(engine) as session:
            session.add(
                CustomerUser(
                    id=uuid4(),
                    email=disabled_email,
                    password_hash=hash_password(PASSWORD),
                    status="disabled",
                )
            )
            session.commit()
        with Session(engine) as session:
            secret = create_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                email=disabled_email,
                role_id=_role_id(workspace_id, "MEMBER"),
            )
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as denied:
                accept_invitation(session, token=secret.token, password=PASSWORD)
            assert denied.value.code == "invite_authentication_failed"
            session.rollback()
    finally:
        _purge((workspace_id,), (owner_email, disabled_email))


def test_quota_reservation_reissue_revoke_and_entitlement_cleanup_split():
    owner_email = f"owner-{uuid4()}@example.test"
    first_email = f"first-{uuid4()}@example.test"
    second_email = f"second-{uuid4()}@example.test"
    inactive_email = f"inactive-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Seat quota", member_limit=2)
    try:
        with Session(engine) as session:
            first = create_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                email=first_email,
                role_id=_role_id(workspace_id, "MEMBER"),
            )
            session.commit()
            first_id = first.invitation.invitation_id
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as quota:
                create_invitation(
                    session,
                    user_id=owner_id,
                    workspace_id=workspace_id,
                    email=second_email,
                    role_id=_role_id(workspace_id, "MEMBER"),
                )
            assert quota.value.code == "member_quota_exceeded"
            session.rollback()
        with Session(engine) as session:
            reissued = reissue_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                invitation_id=first_id,
            )
            assert reissued.token != first.token
            session.commit()
            current_id = reissued.invitation.invitation_id
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as old_link:
                invitation_preview(session, token=first.token)
            assert old_link.value.code == "invite_invalid_or_expired"
            revoke_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                invitation_id=current_id,
            )
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as revoked_link:
                invitation_preview(session, token=reissued.token)
            assert revoked_link.value.code == "invite_invalid_or_expired"
            expired = create_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                email=second_email,
                role_id=_role_id(workspace_id, "MEMBER"),
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as expired_link:
                invitation_preview(
                    session,
                    token=expired.token,
                    now=datetime(2026, 1, 9, tzinfo=UTC),
                )
            assert expired_link.value.code == "invite_invalid_or_expired"
        inactive_user_id, inactive_membership_id = _add_user_membership(
            workspace_id, inactive_email, status="suspended"
        )
        with Session(engine) as session:
            entitlement = session.scalar(
                sa.select(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id == workspace_id,
                    WorkspaceEntitlement.entitlement_key == "workspace_members.enabled",
                )
            )
            entitlement.enabled = False
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as disabled:
                change_member_status(
                    session,
                    user_id=owner_id,
                    workspace_id=workspace_id,
                    membership_id=inactive_membership_id,
                    status="active",
                )
            assert disabled.value.code == "entitlement_blocked"
            session.rollback()
            revoked = change_member_status(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                membership_id=inactive_membership_id,
                status="revoked",
            )
            assert revoked.status == "revoked"
            session.commit()
    finally:
        _purge((workspace_id,), (owner_email, first_email, second_email, inactive_email))


def test_last_owner_admin_authority_role_immediacy_and_session_cleanup():
    owner_email = f"owner-{uuid4()}@example.test"
    admin_email = f"admin-{uuid4()}@example.test"
    member_email = f"member-{uuid4()}@example.test"
    other_owner_email = f"other-owner-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Owner safety")
    _other_owner_id, other_workspace_id = _bootstrap(
        other_owner_email, "Other Workspace"
    )
    admin_id, admin_membership = _add_user_membership(
        workspace_id, admin_email, role_key="ADMIN"
    )
    member_id, member_membership = _add_user_membership(workspace_id, member_email)
    _grant_existing_membership(member_id, other_workspace_id)
    try:
        owner_membership = None
        with Session(engine) as session:
            owner_membership = session.scalar(
                sa.select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == owner_id,
                )
            )
            with pytest.raises(ActionDenied) as last_owner:
                change_member_role(
                    session,
                    user_id=owner_id,
                    workspace_id=workspace_id,
                    membership_id=owner_membership,
                    role_id=_role_id(workspace_id, "MEMBER"),
                )
            assert last_owner.value.code == "last_owner_required"
            session.rollback()
        for requested_status in ("suspended", "revoked"):
            with Session(engine) as session:
                with pytest.raises(ActionDenied) as self_denied:
                    change_member_status(
                        session,
                        user_id=owner_id,
                        workspace_id=workspace_id,
                        membership_id=owner_membership,
                        status=requested_status,
                    )
                assert self_denied.value.code == "self_membership_action_denied"
                session.rollback()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as admin_denied:
                change_member_role(
                    session,
                    user_id=admin_id,
                    workspace_id=workspace_id,
                    membership_id=owner_membership,
                    role_id=_role_id(workspace_id, "ADMIN"),
                )
            assert admin_denied.value.code == "owner_authority_required"
            session.rollback()
        with Session(engine) as session:
            member = session.get(CustomerUser, member_id)
            _token, _csrf_value, record = create_customer_session(
                session, user=member, active_workspace_id=workspace_id
            )
            session_id = record.id
            _other_token, _other_csrf, other_record = create_customer_session(
                session, user=member, active_workspace_id=other_workspace_id
            )
            other_session_id = other_record.id
            session.commit()
        with Session(engine) as session:
            promoted = change_member_role(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                membership_id=member_membership,
                role_id=_role_id(workspace_id, "ADMIN"),
            )
            assert promoted.role_key == "ADMIN"
            suspended = change_member_status(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                membership_id=member_membership,
                status="suspended",
            )
            assert suspended.status == "suspended"
            session.commit()
        with Session(engine) as session:
            assert session.get(CustomerSession, session_id).active_workspace_id is None
            assert (
                session.get(CustomerSession, other_session_id).active_workspace_id
                == other_workspace_id
            )
    finally:
        _purge(
            (workspace_id, other_workspace_id),
            (owner_email, other_owner_email, admin_email, member_email),
        )


def test_tenant_isolation_and_foreign_role_rejected():
    email_a = f"owner-a-{uuid4()}@example.test"
    email_b = f"owner-b-{uuid4()}@example.test"
    member_email = f"member-{uuid4()}@example.test"
    owner_a, workspace_a = _bootstrap(email_a, "Tenant A")
    _owner_b, workspace_b = _bootstrap(email_b, "Tenant B")
    _member_id, membership_id = _add_user_membership(workspace_a, member_email)
    try:
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as foreign_role:
                change_member_role(
                    session,
                    user_id=owner_a,
                    workspace_id=workspace_a,
                    membership_id=membership_id,
                    role_id=_role_id(workspace_b, "ADMIN"),
                )
            assert foreign_role.value.code == "foreign_role"
            session.rollback()
            invitation = WorkspaceInvitation(
                id=uuid4(),
                workspace_id=workspace_a,
                email=f"foreign-{uuid4()}@example.test",
                role_id=_role_id(workspace_b, "MEMBER"),
                invited_by_user_id=owner_a,
                token_hash="a" * 64,
                status="PENDING",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            session.add(invitation)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
    finally:
        _purge((workspace_a, workspace_b), (email_a, email_b, member_email))


def test_concurrent_owner_demotions_leave_one_owner():
    email_a = f"owner-a-{uuid4()}@example.test"
    email_b = f"owner-b-{uuid4()}@example.test"
    owner_a, workspace_id = _bootstrap(email_a, "Concurrent owners")
    owner_b, membership_b = _add_user_membership(
        workspace_id, email_b, role_key="OWNER"
    )
    try:
        with Session(engine) as session:
            membership_a = session.scalar(
                sa.select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == owner_a,
                )
            )
        barrier = Barrier(2)

        def demote(actor_id: UUID, membership_id: UUID) -> str:
            with Session(engine) as session:
                barrier.wait()
                try:
                    change_member_role(
                        session,
                        user_id=actor_id,
                        workspace_id=workspace_id,
                        membership_id=membership_id,
                        role_id=_role_id(workspace_id, "MEMBER"),
                    )
                    session.commit()
                    return "success"
                except ActionDenied as exc:
                    session.rollback()
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: demote(*args),
                    ((owner_a, membership_a), (owner_b, membership_b)),
                )
            )
        assert sorted(results) == ["last_owner_required", "success"]
        with Session(engine) as session:
            owners = session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkspaceMembership)
                .join(WorkspaceRole, WorkspaceRole.id == WorkspaceMembership.role_id)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.status == "active",
                    WorkspaceRole.role_key == "OWNER",
                )
            )
            assert owners == 1
    finally:
        _purge((workspace_id,), (email_a, email_b))


def test_concurrent_invitation_accept_creates_one_identity_and_membership():
    owner_email = f"owner-{uuid4()}@example.test"
    invited_email = f"concurrent-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Concurrent accept")
    try:
        with Session(engine) as session:
            secret = create_invitation(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                email=invited_email,
                role_id=_role_id(workspace_id, "MEMBER"),
            )
            session.commit()
        barrier = Barrier(2)

        def accept() -> str:
            with Session(engine) as session:
                barrier.wait()
                try:
                    accept_invitation(
                        session,
                        token=secret.token,
                        password=PASSWORD,
                        password_confirmation=PASSWORD,
                    )
                    session.commit()
                    return "success"
                except ActionDenied as exc:
                    session.rollback()
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _item: accept(), range(2)))
        assert sorted(results) == ["invite_invalid_or_expired", "success"]
        with Session(engine) as session:
            user_ids = tuple(
                session.scalars(
                    sa.select(CustomerUser.id).where(CustomerUser.email == invited_email)
                ).all()
            )
            assert len(user_ids) == 1
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_ids[0],
                )
            ) == 1
    finally:
        _purge((workspace_id,), (owner_email, invited_email))


def test_settings_usage_rename_html_api_and_invite_csrf():
    owner_email = f"owner-{uuid4()}@example.test"
    invited_email = f"browser-{uuid4()}@example.test"
    owner_id, workspace_id = _bootstrap(owner_email, "Before rename", member_limit=3)
    try:
        with Session(engine) as session:
            settings = get_workspace_settings(
                session, user_id=owner_id, workspace_id=workspace_id
            )
            assert settings.usage.members.active == 1
            assert settings.usage.members.pending_invitations == 0
            assert settings.usage.members.limit == 3
            update_workspace_name(
                session,
                user_id=owner_id,
                workspace_id=workspace_id,
                name="  Renamed   Workspace  ",
            )
            session.commit()

        app = create_app(
            public_repository=EmptyPublicRepository(),
            session_factory=SessionLocal,
            public_origin="https://public.example.test",
        )
        with TestClient(app) as owner_client:
            login_page = owner_client.get("/login")
            login = owner_client.post(
                "/login",
                data={
                    "csrf": _csrf(login_page.text),
                    "email": owner_email,
                    "password": PASSWORD,
                    "return_to": "/app",
                },
                follow_redirects=False,
            )
            assert login.status_code == 303
            users = owner_client.get("/app/users")
            settings_page = owner_client.get("/app/settings")
            assert users.status_code == 200 and "Пользователи" in users.text
            assert settings_page.status_code == 200 and "Renamed Workspace" in settings_page.text
            denied = owner_client.post(
                "/app/api/invitations",
                json={"email": invited_email, "role_id": str(_role_id(workspace_id, "ADMIN"))},
            )
            assert denied.status_code == 403
            csrf_token = owner_client.get("/app/api/csrf").json()["csrf_token"]
            created = owner_client.post(
                "/app/api/invitations",
                headers={"x-csrf-token": csrf_token},
                json={"email": invited_email, "role_id": str(_role_id(workspace_id, "ADMIN"))},
            )
            assert created.status_code == 201
            invite_url = created.json()["invite_url"]
            assert invited_email not in invite_url

        with TestClient(app) as invited_client:
            page = invited_client.get(invite_url)
            assert page.status_code == 200
            no_csrf = invited_client.post(
                invite_url,
                data={"password": PASSWORD, "password_confirmation": PASSWORD},
            )
            assert no_csrf.status_code == 403
            page = invited_client.get(invite_url)
            accepted = invited_client.post(
                invite_url,
                data={
                    "csrf": _csrf(page.text),
                    "password": PASSWORD,
                    "password_confirmation": PASSWORD,
                },
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            assert accepted.headers["location"] == "/app"
            context = invited_client.get("/app/api/context")
            assert context.status_code == 200
            assert context.json()["workspace"] == {
                "name": "Renamed Workspace",
                "role": "ADMIN",
            }
    finally:
        _purge((workspace_id,), (owner_email, invited_email))


def test_migration_documents_backfill_and_partial_pending_uniqueness():
    text = Path(
        "migrations/versions/b5d7f9a1c3e6_add_workspace_users_settings.py"
    ).read_text()
    assert 'down_revision = "a4c6e8f0b2d4"' in text
    assert "ON CONFLICT (role_id, capability_key) DO NOTHING" in text
    assert "workspace.members.invite" in text
    assert "workspace.settings.manage" in text
    assert "uq_workspace_invitation_pending_email" in text
