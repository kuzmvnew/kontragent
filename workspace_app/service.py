"""Workspace P0 authorization, tenant isolation and Saved Company service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.monitoring import MonitoringSubscription
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
    "report.view",
    "report.generate",
    "report.export",
    "bulk.view",
    "bulk.create",
    "bulk.manage",
    "bulk.export",
    "workspace.members.manage",
    "workspace.members.invite",
    "workspace.settings.manage",
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
        "report.view",
        "report.generate",
        "report.export",
        "bulk.view",
        "bulk.create",
        "bulk.manage",
        "bulk.export",
    ),
}

PERMISSION_ENTITLEMENTS = {
    "workspace.view": "workspace.core.enabled",
    "company.search": "workspace.core.enabled",
    "company.view": "workspace.core.enabled",
    "company.save": "saved_companies.enabled",
    "company.unsave": "saved_companies.enabled",
    "monitoring.manage": "monitoring.enabled",
    # Historical reports remain accessible when new generation is disabled.
    "report.view": "workspace.core.enabled",
    "report.generate": "reports.enabled",
    "report.export": "workspace.core.enabled",
    "bulk.view": "workspace.core.enabled",
    "bulk.create": "bulk_check.enabled",
    "bulk.manage": "bulk_check.enabled",
    "bulk.export": "workspace.core.enabled",
    # Security cleanup must remain possible even when commercial seat growth is off.
    "workspace.members.manage": "workspace.core.enabled",
    "workspace.members.invite": "workspace_members.enabled",
    "workspace.settings.manage": "workspace.core.enabled",
}

DEFAULT_ENTITLEMENTS = {
    "workspace.core.enabled": True,
    "saved_companies.enabled": True,
    "monitoring.enabled": False,
    "reports.enabled": True,
    "bulk_check.enabled": True,
    "workspace_members.enabled": True,
}
BOOTSTRAP_POLICY_VERSION = "workspace-bulk-check-v1"
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
    monitoring_state: str = "NOT_ENABLED"
    monitoring_subscription_id: UUID | None = None
    monitoring_last_checked_at: datetime | None = None


@dataclass(frozen=True)
class SavedQuotaUsage:
    enabled: bool
    used: int
    limit: int | None
    remaining: int | None


@dataclass(frozen=True)
class CustomerAuthenticationAttempt:
    resolved_user: CustomerUser | None
    authenticated_user: CustomerUser | None
    identity_ref: str


def login_identity_ref(email: str) -> str:
    """Return a bounded, one-way audit reference without persisting an email."""

    raw = " ".join(str(email or "").strip().split()).casefold()[:320]
    try:
        raw = normalize_email(raw)
    except ValueError:
        pass
    digest = hashlib.sha256(f"workspace-login-v1\0{raw}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def authenticate_customer_attempt(
    session: Session,
    email: str,
    password: str,
) -> CustomerAuthenticationAttempt:
    identity_ref = login_identity_ref(email)
    try:
        normalized = normalize_email(email)
    except ValueError:
        return CustomerAuthenticationAttempt(None, None, identity_ref)
    user = session.scalar(sa.select(CustomerUser).where(CustomerUser.email == normalized))
    password_hash = (
        user.password_hash
        if user is not None and user.status == "active"
        else _DUMMY_PASSWORD_HASH
    )
    password_valid = verify_password(password, password_hash)
    authenticated_user = (
        user if user is not None and user.status == "active" and password_valid else None
    )
    return CustomerAuthenticationAttempt(user, authenticated_user, identity_ref)


def authenticate_customer(session: Session, email: str, password: str) -> CustomerUser | None:
    return authenticate_customer_attempt(session, email, password).authenticated_user


def _active_workspace_is_attributable(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> bool:
    return session.scalar(
        sa.select(WorkspaceMembership.id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == "active",
            Workspace.status == "active",
        )
    ) is not None


def record_login_audit(
    session: Session,
    *,
    attempt: CustomerAuthenticationAttempt,
    workspace_id: UUID | None,
    outcome: str,
) -> None:
    if outcome not in {"success", "denied"}:
        raise ValueError("unsupported login audit outcome")
    if outcome == "success":
        user = attempt.authenticated_user
        if user is None:
            raise ValueError("successful login audit requires an authenticated user")
        if workspace_id is not None and not _active_workspace_is_attributable(
            session,
            user_id=user.id,
            workspace_id=workspace_id,
        ):
            raise ValueError("login workspace is not attributable to the user")
    else:
        user = attempt.resolved_user
        if workspace_id is not None:
            raise ValueError("denied login cannot be attributed to a workspace")
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=user.id if user is not None else None,
            action="auth.login",
            target_type="login_identity",
            target_ref=attempt.identity_ref,
            outcome=outcome,
        )
    )


def record_workspace_selection_audit(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID | None,
    outcome: str,
) -> None:
    if outcome == "success":
        if workspace_id is None or not _active_workspace_is_attributable(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
        ):
            raise ValueError("workspace selection is not attributable to the user")
    elif outcome == "denied":
        if workspace_id is not None:
            raise ValueError("denied selection cannot be attributed to a workspace")
    else:
        raise ValueError("unsupported workspace selection audit outcome")
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=user_id,
            action="workspace.select",
            target_type="workspace",
            target_ref="self",
            outcome=outcome,
        )
    )


def record_logout_audit(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID | None,
) -> None:
    attributable_workspace_id = workspace_id
    if workspace_id is not None and not _active_workspace_is_attributable(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
    ):
        attributable_workspace_id = None
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=attributable_workspace_id,
            actor_user_id=user_id,
            action="auth.logout",
            target_type="customer_session",
            target_ref="self",
            outcome="success",
        )
    )


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
                    else (1000 if entitlement_key == "bulk_check.enabled" else None)
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
    session.scalar(
        sa.select(SavedCompany.id)
        .where(
            SavedCompany.workspace_id == workspace_id,
            SavedCompany.company_id == company.id,
        )
        .with_for_update()
    )
    active_subscription = session.scalar(
        sa.select(MonitoringSubscription.id).where(
            MonitoringSubscription.workspace_id == workspace_id,
            MonitoringSubscription.company_id == company.id,
            MonitoringSubscription.status == "ACTIVE",
        )
    )
    if active_subscription is not None:
        raise ActionDenied(
            "monitoring_active",
            "Сначала приостановите мониторинг компании.",
            status_code=409,
        )
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
    query: str = "",
    limit: int = 100,
) -> tuple[SavedCompanyView, ...]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    normalized_query = " ".join(str(query or "").split())[:160]
    bounded_limit = max(1, min(int(limit), 200))
    statement = (
        sa.select(SavedCompany, Company, MonitoringSubscription)
        .join(Company, Company.id == SavedCompany.company_id)
        .outerjoin(
            MonitoringSubscription,
            sa.and_(
                MonitoringSubscription.workspace_id == SavedCompany.workspace_id,
                MonitoringSubscription.company_id == SavedCompany.company_id,
            ),
        )
        .where(SavedCompany.workspace_id == workspace_id)
        .order_by(SavedCompany.created_at.desc(), Company.inn)
        .limit(bounded_limit)
    )
    if normalized_query:
        statement = statement.where(
            sa.or_(
                Company.inn.contains(normalized_query, autoescape=True),
                Company.name.icontains(normalized_query, autoescape=True),
                Company.short_name.icontains(normalized_query, autoescape=True),
            )
        )
    rows = session.execute(statement).all()
    return tuple(
        SavedCompanyView(
            saved_company_id=saved.id,
            inn=company.inn,
            name=company.short_name or company.name,
            created_at=saved.created_at,
            note=saved.note,
            monitoring_state=subscription.status if subscription is not None else "NOT_ENABLED",
            monitoring_subscription_id=subscription.id if subscription is not None else None,
            monitoring_last_checked_at=(
                subscription.last_checked_at if subscription is not None else None
            ),
        )
        for saved, company, subscription in rows
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
        sa.select(SavedCompany, Company, MonitoringSubscription)
        .join(Company, Company.id == SavedCompany.company_id)
        .outerjoin(
            MonitoringSubscription,
            sa.and_(
                MonitoringSubscription.workspace_id == SavedCompany.workspace_id,
                MonitoringSubscription.company_id == SavedCompany.company_id,
            ),
        )
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
    saved, company, subscription = row
    return SavedCompanyView(
        saved_company_id=saved.id,
        inn=company.inn,
        name=company.short_name or company.name,
        created_at=saved.created_at,
        note=saved.note,
        monitoring_state=subscription.status if subscription is not None else "NOT_ENABLED",
        monitoring_subscription_id=subscription.id if subscription is not None else None,
        monitoring_last_checked_at=(
            subscription.last_checked_at if subscription is not None else None
        ),
    )


def saved_company_for_inn(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    inn: str,
) -> SavedCompanyView | None:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.view",
    )
    company = resolve_legal_company(session, inn)
    row = session.execute(
        sa.select(SavedCompany, MonitoringSubscription)
        .outerjoin(
            MonitoringSubscription,
            sa.and_(
                MonitoringSubscription.workspace_id == SavedCompany.workspace_id,
                MonitoringSubscription.company_id == SavedCompany.company_id,
            ),
        )
        .where(
            SavedCompany.workspace_id == workspace_id,
            SavedCompany.company_id == company.id,
        )
    ).one_or_none()
    if row is None:
        return None
    saved, subscription = row
    return SavedCompanyView(
        saved_company_id=saved.id,
        inn=company.inn,
        name=company.short_name or company.name,
        created_at=saved.created_at,
        note=saved.note,
        monitoring_state=subscription.status if subscription is not None else "NOT_ENABLED",
        monitoring_subscription_id=subscription.id if subscription is not None else None,
        monitoring_last_checked_at=(
            subscription.last_checked_at if subscription is not None else None
        ),
    )


SAVED_NOTE_MAX_LENGTH = 2_000


def normalize_saved_note(value: str | None) -> str | None:
    normalized = " ".join(str(value or "").split())
    if len(normalized) > SAVED_NOTE_MAX_LENGTH:
        raise ActionDenied(
            "saved_note_too_long",
            f"Заметка не может быть длиннее {SAVED_NOTE_MAX_LENGTH} символов.",
            status_code=422,
        )
    return normalized or None


def update_saved_company_note(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    saved_company_id: UUID,
    note: str | None,
) -> tuple[SavedCompanyView, bool]:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="company.save",
    )
    row = session.execute(
        sa.select(SavedCompany, Company)
        .join(Company, Company.id == SavedCompany.company_id)
        .where(
            SavedCompany.id == saved_company_id,
            SavedCompany.workspace_id == workspace_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise ActionDenied(
            "saved_company_not_found",
            "Сохранённая компания не найдена.",
            status_code=404,
        )
    saved, company = row
    normalized_note = normalize_saved_note(note)
    changed = saved.note != normalized_note
    if changed:
        saved.note = normalized_note
    _audit_saved_company_write(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        action=(
            "company.saved_note.clear"
            if normalized_note is None
            else "company.saved_note.update"
        ),
        inn=company.inn,
        outcome="success" if changed else "unchanged",
    )
    session.flush()
    subscription = session.scalar(
        sa.select(MonitoringSubscription).where(
            MonitoringSubscription.workspace_id == workspace_id,
            MonitoringSubscription.company_id == company.id,
        )
    )
    return (
        SavedCompanyView(
            saved_company_id=saved.id,
            inn=company.inn,
            name=company.short_name or company.name,
            created_at=saved.created_at,
            note=saved.note,
            monitoring_state=(
                subscription.status if subscription is not None else "NOT_ENABLED"
            ),
            monitoring_subscription_id=(
                subscription.id if subscription is not None else None
            ),
            monitoring_last_checked_at=(
                subscription.last_checked_at if subscription is not None else None
            ),
        ),
        changed,
    )


def saved_quota_usage(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> SavedQuotaUsage:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    entitlement = session.scalar(
        sa.select(WorkspaceEntitlement).where(
            WorkspaceEntitlement.workspace_id == workspace_id,
            WorkspaceEntitlement.entitlement_key == "saved_companies.enabled",
        )
    )
    used = int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(SavedCompany)
            .where(SavedCompany.workspace_id == workspace_id)
        )
        or 0
    )
    enabled = bool(entitlement is not None and entitlement.enabled)
    limit = entitlement.limit_value if entitlement is not None else None
    remaining = None if limit is None else max(limit - used, 0)
    return SavedQuotaUsage(
        enabled=enabled,
        used=used,
        limit=limit,
        remaining=remaining,
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
