"""Workspace member, role and invitation lifecycle.

CustomerUser is the global identity.  This service only creates or changes
WorkspaceMembership access and uses the persisted system-role capabilities.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.workspace import (
    CustomerSession,
    CustomerUser,
    Workspace,
    WorkspaceAuditEvent,
    WorkspaceEntitlement,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceRoleCapability,
)
from workspace_app.auth import hash_password, normalize_email, verify_password
from workspace_app.service import ActionDenied, authorize


INVITATION_TTL = timedelta(days=7)
SYSTEM_ROLE_KEYS = ("OWNER", "ADMIN", "MEMBER")
MEMBER_LIST_LIMIT = 500
INVITATION_LIST_LIMIT = 500


@dataclass(frozen=True)
class MemberView:
    membership_id: UUID
    user_id: UUID
    email: str
    role_id: UUID
    role_key: str
    role_name: str
    status: str
    joined_at: datetime
    is_current_user: bool


@dataclass(frozen=True)
class InvitationView:
    invitation_id: UUID
    email: str
    role_id: UUID
    role_key: str
    role_name: str
    status: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RoleView:
    role_id: UUID
    role_key: str
    role_name: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class InvitationSecret:
    invitation: InvitationView
    token: str


@dataclass(frozen=True)
class InvitationPreview:
    invitation_id: UUID
    workspace_id: UUID
    workspace_name: str
    email: str
    role_key: str
    role_name: str
    existing_user: bool


@dataclass(frozen=True)
class AcceptedInvitation:
    invitation_id: UUID
    user: CustomerUser
    workspace_id: UUID
    role_key: str


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.now(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _audit(
    session: Session,
    *,
    workspace_id: UUID,
    actor_user_id: UUID,
    action: str,
    target_type: str,
    target_ref: UUID,
    outcome: str = "success",
) -> None:
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_ref=str(target_ref),
            outcome=outcome,
        )
    )


def _scoped_role(session: Session, *, workspace_id: UUID, role_id: UUID) -> WorkspaceRole:
    role = session.scalar(
        sa.select(WorkspaceRole).where(
            WorkspaceRole.workspace_id == workspace_id,
            WorkspaceRole.id == role_id,
            WorkspaceRole.role_key.in_(SYSTEM_ROLE_KEYS),
            WorkspaceRole.is_system.is_(True),
        )
    )
    if role is None:
        raise ActionDenied("foreign_role", "Роль не найдена.", status_code=404)
    return role


def _actor_role(session: Session, *, user_id: UUID, workspace_id: UUID) -> WorkspaceRole:
    role = session.scalar(
        sa.select(WorkspaceRole)
        .join(WorkspaceMembership, WorkspaceMembership.role_id == WorkspaceRole.id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.status == "active",
            WorkspaceRole.workspace_id == workspace_id,
        )
    )
    if role is None:
        raise ActionDenied("membership_required", "Нет активного доступа к рабочему пространству.")
    return role


def _scoped_membership(
    session: Session,
    *,
    workspace_id: UUID,
    membership_id: UUID,
    lock: bool = False,
) -> tuple[WorkspaceMembership, WorkspaceRole, CustomerUser]:
    query = (
        sa.select(WorkspaceMembership, WorkspaceRole, CustomerUser)
        .join(
            WorkspaceRole,
            sa.and_(
                WorkspaceRole.id == WorkspaceMembership.role_id,
                WorkspaceRole.workspace_id == WorkspaceMembership.workspace_id,
            ),
        )
        .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.id == membership_id,
        )
    )
    if lock:
        query = query.with_for_update(of=WorkspaceMembership)
    row = session.execute(query).one_or_none()
    if row is None:
        raise ActionDenied("member_not_found", "Пользователь не найден.", status_code=404)
    return row[0], row[1], row[2]


def _invitation_view(
    invitation: WorkspaceInvitation,
    role: WorkspaceRole,
    *,
    now: datetime,
) -> InvitationView:
    status = (
        "EXPIRED"
        if invitation.status == "PENDING" and invitation.expires_at <= now
        else invitation.status
    )
    return InvitationView(
        invitation_id=invitation.id,
        email=invitation.email,
        role_id=role.id,
        role_key=role.role_key,
        role_name=role.name,
        status=status,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
    )


def list_members(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> tuple[MemberView, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    rows = session.execute(
        sa.select(WorkspaceMembership, CustomerUser, WorkspaceRole)
        .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
        .join(
            WorkspaceRole,
            sa.and_(
                WorkspaceRole.id == WorkspaceMembership.role_id,
                WorkspaceRole.workspace_id == WorkspaceMembership.workspace_id,
            ),
        )
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(
            sa.case((WorkspaceMembership.status == "active", 0), else_=1),
            CustomerUser.email,
            WorkspaceMembership.id,
        )
        .limit(MEMBER_LIST_LIMIT)
    ).all()
    return tuple(
        MemberView(
            membership_id=membership.id,
            user_id=member.id,
            email=member.email,
            role_id=role.id,
            role_key=role.role_key,
            role_name=role.name,
            status=membership.status,
            joined_at=membership.created_at,
            is_current_user=member.id == user_id,
        )
        for membership, member, role in rows
    )


def list_invitations(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    now: datetime | None = None,
) -> tuple[InvitationView, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    current = _now(now)
    rows = session.execute(
        sa.select(WorkspaceInvitation, WorkspaceRole)
        .join(
            WorkspaceRole,
            sa.and_(
                WorkspaceRole.id == WorkspaceInvitation.role_id,
                WorkspaceRole.workspace_id == WorkspaceInvitation.workspace_id,
            ),
        )
        .where(WorkspaceInvitation.workspace_id == workspace_id)
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
        .limit(INVITATION_LIST_LIMIT)
    ).all()
    return tuple(_invitation_view(invitation, role, now=current) for invitation, role in rows)


def list_system_roles(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> tuple[RoleView, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    rows = session.execute(
        sa.select(
            WorkspaceRole.id,
            WorkspaceRole.role_key,
            WorkspaceRole.name,
            WorkspaceRoleCapability.capability_key,
        )
        .outerjoin(
            WorkspaceRoleCapability,
            WorkspaceRoleCapability.role_id == WorkspaceRole.id,
        )
        .where(
            WorkspaceRole.workspace_id == workspace_id,
            WorkspaceRole.role_key.in_(SYSTEM_ROLE_KEYS),
            WorkspaceRole.is_system.is_(True),
        )
        .order_by(
            sa.case(
                (WorkspaceRole.role_key == "OWNER", 0),
                (WorkspaceRole.role_key == "ADMIN", 1),
                else_=2,
            ),
            WorkspaceRoleCapability.capability_key,
        )
    ).all()
    grouped: dict[UUID, dict] = {}
    for role_id, role_key, role_name, capability in rows:
        entry = grouped.setdefault(
            role_id,
            {"role_key": role_key, "role_name": role_name, "capabilities": []},
        )
        if capability is not None:
            entry["capabilities"].append(capability)
    return tuple(
        RoleView(
            role_id=role_id,
            role_key=value["role_key"],
            role_name=value["role_name"],
            capabilities=tuple(value["capabilities"]),
        )
        for role_id, value in grouped.items()
    )


def _seat_usage(session: Session, *, workspace_id: UUID, now: datetime) -> int:
    active_members = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.status == "active",
            )
        )
        or 0
    )
    pending_invitations = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.status == "PENDING",
                WorkspaceInvitation.expires_at > now,
            )
        )
        or 0
    )
    return active_members + pending_invitations


def _enforce_seat_capacity(
    session: Session,
    *,
    workspace_id: UUID,
    limit: int | None,
    now: datetime,
) -> None:
    if limit is not None and _seat_usage(session, workspace_id=workspace_id, now=now) >= limit:
        raise ActionDenied("member_quota_exceeded", "Достигнут лимит пользователей Workspace.")


def _check_invitation_role_policy(actor_role: WorkspaceRole, invited_role: WorkspaceRole) -> None:
    if actor_role.role_key == "ADMIN" and invited_role.role_key == "OWNER":
        raise ActionDenied("owner_authority_required", "Назначить владельца может только владелец.")


def _existing_membership_for_email(
    session: Session,
    *,
    workspace_id: UUID,
    email: str,
) -> WorkspaceMembership | None:
    return session.scalar(
        sa.select(WorkspaceMembership)
        .join(CustomerUser, CustomerUser.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            CustomerUser.email == email,
        )
    )


def create_invitation(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    email: str,
    role_id: UUID,
    now: datetime | None = None,
) -> InvitationSecret:
    current = _now(now)
    context = authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.members.invite",
        lock_entitlement=True,
    )
    try:
        normalized_email = normalize_email(email)
    except ValueError as exc:
        raise ActionDenied("invalid_email", "Укажите корректный email.", status_code=400) from exc
    role = _scoped_role(session, workspace_id=workspace_id, role_id=role_id)
    actor_role = _actor_role(session, user_id=user_id, workspace_id=workspace_id)
    _check_invitation_role_policy(actor_role, role)

    membership = _existing_membership_for_email(
        session,
        workspace_id=workspace_id,
        email=normalized_email,
    )
    if membership is not None:
        code = "already_member" if membership.status == "active" else "member_not_reactivatable"
        message = (
            "Пользователь уже состоит в Workspace."
            if membership.status == "active"
            else "Используйте восстановление существующего доступа."
        )
        raise ActionDenied(code, message, status_code=409)

    pending = session.scalar(
        sa.select(WorkspaceInvitation.id).where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.email == normalized_email,
            WorkspaceInvitation.status == "PENDING",
        )
    )
    if pending is not None:
        raise ActionDenied("invite_already_pending", "Для этого email уже есть приглашение.", status_code=409)

    _enforce_seat_capacity(
        session,
        workspace_id=workspace_id,
        limit=context.quota_limit,
        now=current,
    )
    raw_token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation(
        id=uuid4(),
        workspace_id=workspace_id,
        email=normalized_email,
        role_id=role.id,
        invited_by_user_id=user_id,
        token_hash=_token_hash(raw_token),
        status="PENDING",
        expires_at=current + INVITATION_TTL,
        created_at=current,
    )
    session.add(invitation)
    session.flush()
    _audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=user_id,
        action="workspace.member.invite",
        target_type="workspace_invitation",
        target_ref=invitation.id,
    )
    session.flush()
    return InvitationSecret(
        invitation=_invitation_view(invitation, role, now=current),
        token=raw_token,
    )


def _scoped_invitation(
    session: Session,
    *,
    workspace_id: UUID,
    invitation_id: UUID,
    lock: bool = False,
) -> tuple[WorkspaceInvitation, WorkspaceRole]:
    query = (
        sa.select(WorkspaceInvitation, WorkspaceRole)
        .join(
            WorkspaceRole,
            sa.and_(
                WorkspaceRole.id == WorkspaceInvitation.role_id,
                WorkspaceRole.workspace_id == WorkspaceInvitation.workspace_id,
            ),
        )
        .where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.id == invitation_id,
        )
    )
    if lock:
        query = query.with_for_update(of=WorkspaceInvitation)
    row = session.execute(query).one_or_none()
    if row is None:
        raise ActionDenied("invite_not_found", "Приглашение не найдено.", status_code=404)
    return row[0], row[1]


def reissue_invitation(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    invitation_id: UUID,
    now: datetime | None = None,
) -> InvitationSecret:
    current = _now(now)
    context = authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.members.invite",
        lock_entitlement=True,
    )
    old, role = _scoped_invitation(
        session,
        workspace_id=workspace_id,
        invitation_id=invitation_id,
        lock=True,
    )
    if old.status != "PENDING":
        raise ActionDenied("invite_not_reissuable", "Приглашение нельзя перевыпустить.", status_code=409)
    actor_role = _actor_role(session, user_id=user_id, workspace_id=workspace_id)
    _check_invitation_role_policy(actor_role, role)
    membership = _existing_membership_for_email(
        session,
        workspace_id=workspace_id,
        email=old.email,
    )
    if membership is not None:
        raise ActionDenied("already_member", "Пользователь уже состоит в Workspace.", status_code=409)

    old.status = "REVOKED"
    old.revoked_at = current
    session.flush()
    _enforce_seat_capacity(
        session,
        workspace_id=workspace_id,
        limit=context.quota_limit,
        now=current,
    )
    raw_token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation(
        id=uuid4(),
        workspace_id=workspace_id,
        email=old.email,
        role_id=old.role_id,
        invited_by_user_id=user_id,
        token_hash=_token_hash(raw_token),
        status="PENDING",
        expires_at=current + INVITATION_TTL,
        created_at=current,
    )
    session.add(invitation)
    session.flush()
    _audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=user_id,
        action="workspace.member.invite_reissue",
        target_type="workspace_invitation",
        target_ref=invitation.id,
    )
    session.flush()
    return InvitationSecret(
        invitation=_invitation_view(invitation, role, now=current),
        token=raw_token,
    )


def revoke_invitation(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    invitation_id: UUID,
    now: datetime | None = None,
) -> InvitationView:
    current = _now(now)
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.members.manage",
    )
    invitation, role = _scoped_invitation(
        session,
        workspace_id=workspace_id,
        invitation_id=invitation_id,
        lock=True,
    )
    if invitation.status != "PENDING":
        raise ActionDenied("invite_not_revocable", "Приглашение нельзя отозвать.", status_code=409)
    invitation.status = "REVOKED"
    invitation.revoked_at = current
    _audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=user_id,
        action="workspace.member.invite_revoke",
        target_type="workspace_invitation",
        target_ref=invitation.id,
    )
    session.flush()
    return _invitation_view(invitation, role, now=current)


def _valid_invitation_by_token(
    session: Session,
    *,
    token: str,
    now: datetime,
    lock: bool = False,
) -> tuple[WorkspaceInvitation, Workspace, WorkspaceRole]:
    query = (
        sa.select(WorkspaceInvitation, Workspace, WorkspaceRole)
        .join(Workspace, Workspace.id == WorkspaceInvitation.workspace_id)
        .join(
            WorkspaceRole,
            sa.and_(
                WorkspaceRole.id == WorkspaceInvitation.role_id,
                WorkspaceRole.workspace_id == WorkspaceInvitation.workspace_id,
            ),
        )
        .where(WorkspaceInvitation.token_hash == _token_hash(token))
    )
    if lock:
        query = query.with_for_update(of=WorkspaceInvitation)
    row = session.execute(query).one_or_none()
    if (
        row is None
        or row[0].status != "PENDING"
        or row[0].expires_at <= now
        or row[1].status != "active"
    ):
        raise ActionDenied(
            "invite_invalid_or_expired",
            "Приглашение недействительно или истекло.",
            status_code=404,
        )
    return row[0], row[1], row[2]


def invitation_preview(
    session: Session,
    *,
    token: str,
    now: datetime | None = None,
) -> InvitationPreview:
    invitation, workspace, role = _valid_invitation_by_token(
        session,
        token=token,
        now=_now(now),
    )
    existing = session.scalar(
        sa.select(CustomerUser.id).where(CustomerUser.email == invitation.email)
    )
    return InvitationPreview(
        invitation_id=invitation.id,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        email=invitation.email,
        role_key=role.role_key,
        role_name=role.name,
        existing_user=existing is not None,
    )


def accept_invitation(
    session: Session,
    *,
    token: str,
    password: str,
    password_confirmation: str | None = None,
    now: datetime | None = None,
) -> AcceptedInvitation:
    current = _now(now)
    invitation, workspace, role = _valid_invitation_by_token(
        session,
        token=token,
        now=current,
        lock=True,
    )
    user = session.scalar(
        sa.select(CustomerUser).where(CustomerUser.email == invitation.email)
    )
    if user is not None:
        if user.status != "active" or not verify_password(password, user.password_hash):
            raise ActionDenied(
                "invite_authentication_failed",
                "Не удалось подтвердить приглашение и учётную запись.",
                status_code=401,
            )
    else:
        if password != str(password_confirmation or ""):
            raise ActionDenied("password_confirmation_mismatch", "Пароли не совпадают.", status_code=400)
        try:
            encoded = hash_password(password)
        except ValueError as exc:
            raise ActionDenied(
                "password_policy_failed",
                "Пароль должен содержать не менее 14 символов.",
                status_code=400,
            ) from exc
        user = CustomerUser(
            id=uuid4(),
            email=invitation.email,
            password_hash=encoded,
            status="active",
        )
        session.add(user)
        session.flush()

    membership = session.scalar(
        sa.select(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
        .with_for_update()
    )
    if membership is None:
        membership = WorkspaceMembership(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role_id=role.id,
            status="active",
            created_at=current,
        )
        session.add(membership)
    elif membership.status == "active":
        raise ActionDenied("already_member", "Пользователь уже состоит в Workspace.", status_code=409)
    else:
        membership.role_id = role.id
        membership.status = "active"

    invitation.status = "ACCEPTED"
    invitation.accepted_at = current
    invitation.accepted_by_user_id = user.id
    session.flush()
    _audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=user.id,
        action="workspace.member.accept",
        target_type="workspace_invitation",
        target_ref=invitation.id,
    )
    session.flush()
    return AcceptedInvitation(
        invitation_id=invitation.id,
        user=user,
        workspace_id=workspace.id,
        role_key=role.role_key,
    )


def _lock_workspace_management(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> WorkspaceRole:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.members.manage",
    )
    workspace = session.scalar(
        sa.select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None or workspace.status != "active":
        raise ActionDenied("workspace_unavailable", "Рабочее пространство недоступно.")
    # Re-check after a concurrent owner-sensitive action releases the canonical lock.
    session.expire_all()
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.members.manage",
    )
    return _actor_role(session, user_id=user_id, workspace_id=workspace_id)


def _active_owner_count(session: Session, *, workspace_id: UUID) -> int:
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(WorkspaceMembership)
            .join(
                WorkspaceRole,
                sa.and_(
                    WorkspaceRole.id == WorkspaceMembership.role_id,
                    WorkspaceRole.workspace_id == WorkspaceMembership.workspace_id,
                ),
            )
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.status == "active",
                WorkspaceRole.role_key == "OWNER",
            )
        )
        or 0
    )


def _require_owner_authority(
    *,
    actor_role: WorkspaceRole,
    target_role: WorkspaceRole,
    requested_role: WorkspaceRole | None = None,
) -> None:
    if actor_role.role_key != "OWNER" and (
        target_role.role_key == "OWNER"
        or (requested_role is not None and requested_role.role_key == "OWNER")
    ):
        raise ActionDenied("owner_authority_required", "Изменять владельца может только владелец.")


def change_member_role(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    membership_id: UUID,
    role_id: UUID,
) -> MemberView:
    actor_role = _lock_workspace_management(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    membership, old_role, member = _scoped_membership(
        session,
        workspace_id=workspace_id,
        membership_id=membership_id,
        lock=True,
    )
    new_role = _scoped_role(session, workspace_id=workspace_id, role_id=role_id)
    _require_owner_authority(
        actor_role=actor_role,
        target_role=old_role,
        requested_role=new_role,
    )
    if old_role.id == new_role.id:
        return MemberView(
            membership.id,
            member.id,
            member.email,
            old_role.id,
            old_role.role_key,
            old_role.name,
            membership.status,
            membership.created_at,
            member.id == user_id,
        )
    if (
        membership.status == "active"
        and old_role.role_key == "OWNER"
        and new_role.role_key != "OWNER"
        and _active_owner_count(session, workspace_id=workspace_id) <= 1
    ):
        raise ActionDenied("last_owner_required", "В Workspace должен остаться активный владелец.", status_code=409)
    membership.role_id = new_role.id
    _audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=user_id,
        action="workspace.member.role_change",
        target_type="workspace_membership",
        target_ref=membership.id,
    )
    session.flush()
    return MemberView(
        membership.id,
        member.id,
        member.email,
        new_role.id,
        new_role.role_key,
        new_role.name,
        membership.status,
        membership.created_at,
        member.id == user_id,
    )


def _clear_active_workspace_sessions(
    session: Session,
    *,
    workspace_id: UUID,
    member_user_id: UUID,
) -> None:
    session.execute(
        sa.update(CustomerSession)
        .where(
            CustomerSession.user_id == member_user_id,
            CustomerSession.active_workspace_id == workspace_id,
        )
        .values(active_workspace_id=None)
    )


def change_member_status(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    membership_id: UUID,
    status: str,
    now: datetime | None = None,
) -> MemberView:
    requested = str(status or "").strip().lower()
    if requested not in {"active", "suspended", "revoked"}:
        raise ActionDenied("member_status_invalid", "Недопустимый статус.", status_code=400)
    current = _now(now)
    actor_role = _lock_workspace_management(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    membership, role, member = _scoped_membership(
        session,
        workspace_id=workspace_id,
        membership_id=membership_id,
        lock=True,
    )
    _require_owner_authority(actor_role=actor_role, target_role=role)

    if requested in {"suspended", "revoked"}:
        if member.id == user_id:
            raise ActionDenied("self_membership_action_denied", "Нельзя отключить собственный доступ.")
        allowed_sources = {"suspended": {"active"}, "revoked": {"active", "suspended"}}
        if membership.status not in allowed_sources[requested]:
            raise ActionDenied("member_status_transition_invalid", "Недопустимый переход статуса.", status_code=409)
        if role.role_key == "OWNER" and membership.status == "active" and _active_owner_count(
            session, workspace_id=workspace_id
        ) <= 1:
            raise ActionDenied("last_owner_required", "В Workspace должен остаться активный владелец.", status_code=409)
        membership.status = requested
        _clear_active_workspace_sessions(
            session,
            workspace_id=workspace_id,
            member_user_id=member.id,
        )
        action = "workspace.member.suspend" if requested == "suspended" else "workspace.member.revoke"
    else:
        if membership.status not in {"suspended", "revoked"}:
            raise ActionDenied("member_not_reactivatable", "Доступ нельзя восстановить.", status_code=409)
        if member.status != "active":
            raise ActionDenied("member_not_reactivatable", "Учётная запись пользователя отключена.", status_code=409)
        entitlement = session.scalar(
            sa.select(WorkspaceEntitlement)
            .where(
                WorkspaceEntitlement.workspace_id == workspace_id,
                WorkspaceEntitlement.entitlement_key == "workspace_members.enabled",
            )
            .with_for_update()
        )
        if entitlement is None or not entitlement.enabled:
            raise ActionDenied("entitlement_blocked", "Добавление пользователей не подключено.")
        _enforce_seat_capacity(
            session,
            workspace_id=workspace_id,
            limit=entitlement.limit_value,
            now=current,
        )
        membership.status = "active"
        action = "workspace.member.reactivate"

    _audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=user_id,
        action=action,
        target_type="workspace_membership",
        target_ref=membership.id,
    )
    session.flush()
    return MemberView(
        membership.id,
        member.id,
        member.email,
        role.id,
        role.role_key,
        role.name,
        membership.status,
        membership.created_at,
        member.id == user_id,
    )
