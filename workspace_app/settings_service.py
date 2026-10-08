"""Workspace settings and bounded tenant-scoped Usage projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.monitoring import MonitoringSubscription
from app.models.workspace import (
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
)
from workspace_app.service import ActionDenied, authorize
from workspace_app.text_validation import has_forbidden_text_character


@dataclass(frozen=True)
class EntitlementUsage:
    key: str
    enabled: bool
    limit: int | None


@dataclass(frozen=True)
class CountLimitUsage:
    used: int
    enabled: bool
    limit: int | None
    remaining: int | None


@dataclass(frozen=True)
class MemberUsage:
    active: int
    pending_invitations: int
    enabled: bool
    limit: int | None
    remaining: int | None


@dataclass(frozen=True)
class MonitoringUsage:
    enabled: bool
    active_subscriptions: int
    paused_subscriptions: int


@dataclass(frozen=True)
class BulkUsage:
    enabled: bool
    job_count: int
    per_job_unique_inn_limit: int | None


@dataclass(frozen=True)
class WorkspaceUsageView:
    workspace_id: UUID
    workspace_status: str
    workspace_created_at: datetime
    members: MemberUsage
    saved: CountLimitUsage
    monitoring: MonitoringUsage
    reports: CountLimitUsage
    bulk: BulkUsage
    entitlements: tuple[EntitlementUsage, ...]


@dataclass(frozen=True)
class WorkspaceSettingsView:
    workspace_id: UUID
    workspace_name: str
    workspace_status: str
    current_user_email: str
    current_role_key: str
    current_role_name: str
    usage: WorkspaceUsageView


def _remaining(limit: int | None, used: int) -> int | None:
    return None if limit is None else max(limit - used, 0)


def get_workspace_usage(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    now: datetime | None = None,
) -> WorkspaceUsageView:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    current = now or datetime.now(UTC)
    counts = session.execute(
        sa.select(
            sa.select(sa.func.count())
            .select_from(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.status == "active",
            )
            .scalar_subquery()
            .label("active_members"),
            sa.select(sa.func.count())
            .select_from(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.status == "PENDING",
                WorkspaceInvitation.expires_at > current,
            )
            .scalar_subquery()
            .label("pending_invitations"),
            sa.select(sa.func.count())
            .select_from(SavedCompany)
            .where(SavedCompany.workspace_id == workspace_id)
            .scalar_subquery()
            .label("saved_companies"),
            sa.select(sa.func.count())
            .select_from(MonitoringSubscription)
            .where(
                MonitoringSubscription.workspace_id == workspace_id,
                MonitoringSubscription.status == "ACTIVE",
            )
            .scalar_subquery()
            .label("active_monitoring"),
            sa.select(sa.func.count())
            .select_from(MonitoringSubscription)
            .where(
                MonitoringSubscription.workspace_id == workspace_id,
                MonitoringSubscription.status == "PAUSED",
            )
            .scalar_subquery()
            .label("paused_monitoring"),
            sa.select(sa.func.count())
            .select_from(WorkspaceReport)
            .where(WorkspaceReport.workspace_id == workspace_id)
            .scalar_subquery()
            .label("reports"),
            sa.select(sa.func.count())
            .select_from(WorkspaceBulkJob)
            .where(WorkspaceBulkJob.workspace_id == workspace_id)
            .scalar_subquery()
            .label("bulk_jobs"),
        )
    ).one()
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise ActionDenied("workspace_unavailable", "Рабочее пространство недоступно.", status_code=404)
    entitlement_rows = session.execute(
        sa.select(
            WorkspaceEntitlement.entitlement_key,
            WorkspaceEntitlement.enabled,
            WorkspaceEntitlement.limit_value,
        )
        .where(WorkspaceEntitlement.workspace_id == workspace_id)
        .order_by(WorkspaceEntitlement.entitlement_key)
    ).all()
    entitlements = tuple(
        EntitlementUsage(key=key, enabled=enabled, limit=limit)
        for key, enabled, limit in entitlement_rows
    )
    entitlement_map = {item.key: item for item in entitlements}

    def entitlement(key: str) -> EntitlementUsage:
        return entitlement_map.get(key, EntitlementUsage(key=key, enabled=False, limit=None))

    member_entitlement = entitlement("workspace_members.enabled")
    saved_entitlement = entitlement("saved_companies.enabled")
    monitoring_entitlement = entitlement("monitoring.enabled")
    report_entitlement = entitlement("reports.enabled")
    bulk_entitlement = entitlement("bulk_check.enabled")
    active_members = int(counts.active_members or 0)
    pending_invitations = int(counts.pending_invitations or 0)
    saved_count = int(counts.saved_companies or 0)
    report_count = int(counts.reports or 0)
    return WorkspaceUsageView(
        workspace_id=workspace.id,
        workspace_status=workspace.status,
        workspace_created_at=workspace.created_at,
        members=MemberUsage(
            active=active_members,
            pending_invitations=pending_invitations,
            enabled=member_entitlement.enabled,
            limit=member_entitlement.limit,
            remaining=_remaining(
                member_entitlement.limit,
                active_members + pending_invitations,
            ),
        ),
        saved=CountLimitUsage(
            used=saved_count,
            enabled=saved_entitlement.enabled,
            limit=saved_entitlement.limit,
            remaining=_remaining(saved_entitlement.limit, saved_count),
        ),
        monitoring=MonitoringUsage(
            enabled=monitoring_entitlement.enabled,
            active_subscriptions=int(counts.active_monitoring or 0),
            paused_subscriptions=int(counts.paused_monitoring or 0),
        ),
        reports=CountLimitUsage(
            used=report_count,
            enabled=report_entitlement.enabled,
            limit=report_entitlement.limit,
            remaining=_remaining(report_entitlement.limit, report_count),
        ),
        bulk=BulkUsage(
            enabled=bulk_entitlement.enabled,
            job_count=int(counts.bulk_jobs or 0),
            per_job_unique_inn_limit=bulk_entitlement.limit,
        ),
        entitlements=entitlements,
    )


def get_workspace_settings(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    now: datetime | None = None,
) -> WorkspaceSettingsView:
    context = authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    workspace = session.get(Workspace, workspace_id)
    role = session.get(WorkspaceRole, context.role_id)
    user = session.get(CustomerUser, user_id)
    if workspace is None or role is None or user is None:
        raise ActionDenied("workspace_unavailable", "Рабочее пространство недоступно.", status_code=404)
    usage = get_workspace_usage(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        now=now,
    )
    return WorkspaceSettingsView(
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        workspace_status=workspace.status,
        current_user_email=user.email,
        current_role_key=role.role_key,
        current_role_name=role.name,
        usage=usage,
    )


def update_workspace_name(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    name: str,
) -> Workspace:
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.settings.manage",
    )
    raw = str(name or "")
    if has_forbidden_text_character(raw):
        raise ActionDenied(
            "workspace_name_invalid_character",
            "Название содержит недопустимые символы.",
            status_code=400,
        )
    normalized = " ".join(raw.split())
    if not normalized:
        raise ActionDenied("workspace_name_required", "Укажите название Workspace.", status_code=400)
    if len(normalized) > 250:
        raise ActionDenied("workspace_name_too_long", "Название не должно превышать 250 символов.", status_code=400)
    workspace = session.scalar(
        sa.select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None or workspace.status != "active":
        raise ActionDenied("workspace_unavailable", "Рабочее пространство недоступно.", status_code=404)
    # Capability changes in a concurrent transaction must be visible after the lock.
    session.expire_all()
    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.settings.manage",
    )
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise ActionDenied("workspace_unavailable", "Рабочее пространство недоступно.", status_code=404)
    workspace.name = normalized
    session.add(
        WorkspaceAuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=user_id,
            action="workspace.settings.update",
            target_type="workspace",
            target_ref=str(workspace_id),
            outcome="success",
        )
    )
    session.flush()
    return workspace
