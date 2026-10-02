"""Workspace P0 authorization, tenant isolation and Saved Company service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.workspace import (
    CustomerUser,
    SavedCompany,
    Workspace,
    WorkspaceAuditEvent,
    WorkspaceEntitlement,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceRoleCapability,
)
from workspace_app.auth import normalize_email, verify_password


P0_PERMISSIONS = (
    "workspace.view",
    "company.search",
    "company.view",
    "company.save",
    "company.unsave",
    "monitoring.manage",
    "workspace.members.manage",
)

ROLE_PERMISSIONS = {
    "OWNER": P0_PERMISSIONS,
    "ADMIN": P0_PERMISSIONS,
    "MEMBER": (
        "workspace.view",
        "company.search",
        "company.view",
        "company.save",
        "company.unsave",
    ),
}

PERMISSION_ENTITLEMENTS = {
    "workspace.view": "workspace.core.enabled",
    "company.search": "workspace.core.enabled",
    "company.view": "workspace.core.enabled",
    "company.save": "saved_companies.enabled",
    "company.unsave": "saved_companies.enabled",
    "monitoring.manage": "monitoring.enabled",
    "workspace.members.manage": "workspace_members.enabled",
}

DEFAULT_ENTITLEMENTS = {
    "workspace.core.enabled": True,
    "saved_companies.enabled": True,
    "monitoring.enabled": False,
    "workspace_members.enabled": True,
}
BOOTSTRAP_POLICY_VERSION = "workspace-p0-bootstrap-v1"
_DUMMY_PASSWORD_HASH = (
    "scrypt-v1$32768$8$1$d29ya3NwYWNlLXAwLWR1bW0$"
    "6dS6Cf3NriX59EuQ2A5PzSdIXkoy34-HM0xzb0U5x6E"
)


class ActionDenied(PermissionError):
    def __init__(self, code: str, message: str, *, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AuthorizationContext:
    user_id: UUID
    workspace_id: UUID
    membership_id: UUID
    role_id: UUID
    permission_key: str
    entitlement_key: str
    entitlement_id: UUID
    quota_limit: int | None


@dataclass(frozen=True)
class SavedCompanyView:
    saved_company_id: UUID
    inn: str
    name: str
    created_at: datetime
    note: str | None


def authenticate_customer(session: Session, email: str, password: str) -> CustomerUser | None:
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    user = session.scalar(sa.select(CustomerUser).where(CustomerUser.email == normalized))
    password_hash = (
        user.password_hash
        if user is not None and user.status == "active"
        else _DUMMY_PASSWORD_HASH
    )
    password_valid = verify_password(password, password_hash)
    return user if user is not None and user.status == "active" and password_valid else None


def bootstrap_workspace_owner(
    session: Session,
    *,
    email: str,
    password_hash: str,
    workspace_name: str,
    saved_company_limit: int | None,
) -> tuple[CustomerUser, Workspace]:
    """Create an explicit controlled P0 owner/workspace.

    This is not public self-registration and does not imply a commercial plan.
    """

    normalized = normalize_email(email)
    name = " ".join(str(workspace_name or "").split())
    if not name or len(name) > 250:
        raise ValueError("workspace name is required")
    if saved_company_limit is not None and saved_company_limit < 0:
        raise ValueError("saved_company_limit must be non-negative")

    existing = session.scalar(sa.select(CustomerUser).where(CustomerUser.email == normalized))
    if existing is not None:
        raise ValueError("customer user already exists; bootstrap is create-only")

    user = CustomerUser(
        id=uuid4(),
        email=normalized,
        password_hash=password_hash,
        status="active",
    )
    workspace = Workspace(id=uuid4(), name=name, status="active")
    roles = {
        role_key: WorkspaceRole(
            id=uuid4(),
            workspace_id=workspace.id,
            role_key=role_key,
            name={
                "OWNER": "Владелец",
                "ADMIN": "Администратор",
                "MEMBER": "Участник",
            }[role_key],
            is_system=True,
        )
        for role_key in ROLE_PERMISSIONS
    }
    session.add_all((user, workspace, *roles.values()))
    session.flush()
    session.add(
        WorkspaceMembership(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role_id=roles["OWNER"].id,
            status="active",
        )
    )
    for role_key, permissions in ROLE_PERMISSIONS.items():
        for permission in permissions:
            session.add(
                WorkspaceRoleCapability(
                    role_id=roles[role_key].id,
                    capability_key=permission,
                )
            )
    for entitlement_key, enabled in DEFAULT_ENTITLEMENTS.items():
        session.add(
            WorkspaceEntitlement(
                id=uuid4(),
                workspace_id=workspace.id,
                entitlement_key=entitlement_key,
                enabled=enabled,
                limit_value=(
                    saved_company_limit
                    if entitlement_key == "saved_companies.enabled"
                    else None
                ),
                policy_version=BOOTSTRAP_POLICY_VERSION,
            )
        )
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace.bootstrap",
            target_type="workspace",
            target_ref="self",
            outcome="success",
        )
    )
    session.flush()
    return user, workspace


def list_active_workspaces(session: Session, user_id: UUID) -> tuple[Workspace, ...]:
    return tuple(
        session.scalars(
            sa.select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.status == "active",
                Workspace.status == "active",
            )
            .order_by(Workspace.name, Workspace.id)
        ).all()
    )


def _membership_and_role(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> tuple[WorkspaceMembership, WorkspaceRole]:
    user = session.get(CustomerUser, user_id)
    if user is None or user.status != "active":
        raise ActionDenied("authentication_required", "Сессия пользователя недействительна.", status_code=401)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "active":
        raise ActionDenied("workspace_unavailable", "Рабочее пространство недоступно.")
    membership = session.scalar(
        sa.select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == "active",
        )
    )
    if membership is None:
        raise ActionDenied("membership_required", "Нет активного доступа к рабочему пространству.")
    role = session.get(WorkspaceRole, membership.role_id)
    if role is None or role.workspace_id != workspace_id:
        raise ActionDenied("resource_scope_denied", "Роль не принадлежит рабочему пространству.")
    return membership, role


def authorize(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    permission_key: str,
    lock_entitlement: bool = False,
) -> AuthorizationContext:
    membership, role = _membership_and_role(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    allowed = session.scalar(
        sa.select(WorkspaceRoleCapability.capability_key).where(
            WorkspaceRoleCapability.role_id == role.id,
            WorkspaceRoleCapability.capability_key == permission_key,
        )
    )
    if allowed is None:
        raise ActionDenied("permission_denied", "Недостаточно прав для этого действия.")

    entitlement_key = PERMISSION_ENTITLEMENTS.get(permission_key)
    if entitlement_key is None:
        raise ActionDenied("permission_unknown", "Неизвестное право доступа.")
    entitlement_query = sa.select(WorkspaceEntitlement).where(
        WorkspaceEntitlement.workspace_id == workspace_id,
        WorkspaceEntitlement.entitlement_key == entitlement_key,
    )
    if lock_entitlement:
        entitlement_query = entitlement_query.with_for_update()
    entitlement = session.scalar(entitlement_query)
    if entitlement is None or not entitlement.enabled:
        raise ActionDenied("entitlement_blocked", "Функция не включена для рабочего пространства.")
    return AuthorizationContext(
        user_id=user_id,
        workspace_id=workspace_id,
        membership_id=membership.id,
        role_id=role.id,
        permission_key=permission_key,
        entitlement_key=entitlement_key,
        entitlement_id=entitlement.id,
        quota_limit=entitlement.limit_value,
    )


def action_state(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    permission_key: str,
) -> tuple[bool, str | None, int | None]:
    try:
        context = authorize(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            permission_key=permission_key,
        )
        return True, None, context.quota_limit
    except ActionDenied as exc:
        return False, exc.code, None


def resolve_legal_company(session: Session, inn: str) -> Company:
    normalized = str(inn or "").strip()
    company = session.scalar(
        sa.select(Company).where(
            Company.inn == normalized,
            Company.entity_type == "legal",
        )
    )
    if company is None:
        raise ActionDenied("company_not_resolved", "Компания не найдена в мастер-модели.", status_code=404)
    return company


def is_saved(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
) -> bool:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    company = resolve_legal_company(session, inn)
    return session.scalar(
        sa.select(SavedCompany.id).where(
            SavedCompany.workspace_id == workspace_id,
            SavedCompany.company_id == company.id,
        )
    ) is not None


def _audit_saved_company_write(
    session: Session,
    *,
    workspace_id: UUID,
    user_id: UUID,
    action: str,
    inn: str,
    outcome: str,
) -> None:
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=user_id,
            action=action,
            target_type="company",
            target_ref=inn,
            outcome=outcome,
        )
    )


def save_company(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
) -> bool:
    context = authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.save",
        lock_entitlement=True,
    )
    company = resolve_legal_company(session, inn)
    existing_id = session.scalar(
        sa.select(SavedCompany.id).where(
            SavedCompany.workspace_id == workspace_id,
            SavedCompany.company_id == company.id,
        )
    )
    if existing_id is not None:
        _audit_saved_company_write(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            action="company.save",
            inn=company.inn,
            outcome="already_saved",
        )
        session.flush()
        return False

    if context.quota_limit is not None:
        used = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(SavedCompany)
                .where(SavedCompany.workspace_id == workspace_id)
            )
            or 0
        )
        if used >= context.quota_limit:
            _audit_saved_company_write(
                session,
                workspace_id=workspace_id,
                user_id=user_id,
                action="company.save",
                inn=company.inn,
                outcome="quota_exceeded",
            )
            session.flush()
            raise ActionDenied("quota_exceeded", "Достигнут лимит сохранённых компаний.")

    saved_id = session.scalar(
        insert(SavedCompany)
        .values(
            id=uuid4(),
            workspace_id=workspace_id,
            company_id=company.id,
            saved_by_user_id=user_id,
        )
        .on_conflict_do_nothing(
            index_elements=[SavedCompany.workspace_id, SavedCompany.company_id]
        )
        .returning(SavedCompany.id)
    )
    outcome = "success" if saved_id is not None else "already_saved"
    _audit_saved_company_write(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action="company.save",
        inn=company.inn,
        outcome=outcome,
    )
    session.flush()
    return saved_id is not None


def unsave_company(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
) -> bool:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.unsave",
    )
    company = resolve_legal_company(session, inn)
    saved_id = session.scalar(
        sa.delete(SavedCompany)
        .where(
            SavedCompany.workspace_id == workspace_id,
            SavedCompany.company_id == company.id,
        )
        .returning(SavedCompany.id)
    )
    if saved_id is None:
        _audit_saved_company_write(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            action="company.unsave",
            inn=company.inn,
            outcome="not_saved",
        )
        session.flush()
        return False
    _audit_saved_company_write(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action="company.unsave",
        inn=company.inn,
        outcome="success",
    )
    session.flush()
    return True


def saved_companies(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> tuple[SavedCompanyView, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    rows = session.execute(
        sa.select(SavedCompany, Company)
        .join(Company, Company.id == SavedCompany.company_id)
        .where(SavedCompany.workspace_id == workspace_id)
        .order_by(SavedCompany.created_at.desc(), Company.inn)
    ).all()
    return tuple(
        SavedCompanyView(
            saved_company_id=saved.id,
            inn=company.inn,
            name=company.short_name or company.name,
            created_at=saved.created_at,
            note=saved.note,
        )
        for saved, company in rows
    )


def saved_company_by_id(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    saved_company_id: UUID,
) -> SavedCompanyView:
    """Load a tenant resource only through its workspace-scoped identity."""

    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    row = session.execute(
        sa.select(SavedCompany, Company)
        .join(Company, Company.id == SavedCompany.company_id)
        .where(
            SavedCompany.id == saved_company_id,
            SavedCompany.workspace_id == workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise ActionDenied(
            "saved_company_not_found",
            "Сохранённая компания не найдена.",
            status_code=404,
        )
    saved, company = row
    return SavedCompanyView(
        saved_company_id=saved.id,
        inn=company.inn,
        name=company.short_name or company.name,
        created_at=saved.created_at,
        note=saved.note,
    )


def saved_count(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> int:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(SavedCompany)
            .where(SavedCompany.workspace_id == workspace_id)
        )
        or 0
    )
