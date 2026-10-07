"""Stable tenant-scoped view model for the Workspace dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from workspace_app.monitoring_service import (
    MonitoringWorkspaceSummary,
    WorkspaceFeedItem,
    get_workspace_monitoring_summary,
    list_workspace_recent_feed,
)
from workspace_app.service import (
    SavedCompanyView,
    SavedQuotaUsage,
    action_state,
    authorize,
    saved_companies,
    saved_quota_usage,
)


@dataclass(frozen=True)
class DashboardMetrics:
    saved_companies_count: int
    saved_companies_limit: int | None
    saved_companies_remaining: int | None
    active_monitoring_count: int
    paused_monitoring_count: int
    unread_monitoring_event_count: int
    total_monitoring_event_count: int


@dataclass(frozen=True)
class WorkspaceDashboard:
    metrics: DashboardMetrics
    quota: SavedQuotaUsage
    recent_saved: tuple[SavedCompanyView, ...]
    recent_events: tuple[WorkspaceFeedItem, ...]
    monitoring_available: bool
    monitoring_denial: str | None


def get_workspace_dashboard(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    recent_limit: int = 5,
) -> WorkspaceDashboard:
    """Build the complete dashboard without unbounded or per-row queries."""

    authorize(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="workspace.view",
    )
    bounded_limit = max(1, min(int(recent_limit), 10))
    quota = saved_quota_usage(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    monitoring_available, monitoring_denial, _monitoring_limit = action_state(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        permission_key="monitoring.manage",
    )
    monitoring = (
        get_workspace_monitoring_summary(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if monitoring_available
        else MonitoringWorkspaceSummary(0, 0, 0, 0)
    )
    recent_events = (
        list_workspace_recent_feed(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            limit=bounded_limit,
        )
        if monitoring_available
        else ()
    )
    return WorkspaceDashboard(
        metrics=DashboardMetrics(
            saved_companies_count=quota.used,
            saved_companies_limit=quota.limit,
            saved_companies_remaining=quota.remaining,
            active_monitoring_count=monitoring.active_count,
            paused_monitoring_count=monitoring.paused_count,
            unread_monitoring_event_count=monitoring.unread_event_count,
            total_monitoring_event_count=monitoring.total_event_count,
        ),
        quota=quota,
        recent_saved=saved_companies(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            limit=bounded_limit,
        ),
        recent_events=recent_events,
        monitoring_available=monitoring_available,
        monitoring_denial=monitoring_denial,
    )
