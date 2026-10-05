from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from app.models.monitoring import (
    CompanyMonitoringSnapshot,
    MonitoringEvent,
    MonitoringSubscription,
    WorkspaceFeedEntry,
)
from app.models.semantic_fact import CompanySemanticFact
from app.models.workspace import (
    WorkspaceAuditEvent,
    WorkspaceEntitlement,
    WorkspaceMembership,
    WorkspaceRole,
)
from tests.public_test_support import projection
from tests.test_workspace_p0 import (
    FakePublicRepository,
    _api_login,
    _bootstrap,
    _cleanup,
    _login,
)
from workspace_app.auth import CSRF_COOKIE
from workspace_app.main import create_app
from workspace_app.monitoring_service import (
    capture_snapshot,
    detect_changes,
    get_monitoring_state,
    list_workspace_feed,
    mark_feed_entry_read,
    monitor_company_once,
    pause_subscription,
    resume_subscription,
    subscribe_company,
)
from workspace_app.service import ActionDenied, save_company, unsave_company


NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def _company(session: Session, inn: str) -> Company:
    company = Company(inn=inn, name=f"Monitoring {inn}", entity_type="legal")
    session.add(company)
    session.flush()
    return company


def _enable_entitlement(session: Session, workspace_id) -> None:
    entitlement = session.scalar(
        sa.select(WorkspaceEntitlement).where(
            WorkspaceEntitlement.workspace_id == workspace_id,
            WorkspaceEntitlement.entitlement_key == "monitoring.enabled",
        )
    )
    assert entitlement is not None
    entitlement.enabled = True
    session.flush()


def _semantic_fact(
    session: Session,
    company_id: int,
    *,
    section: str = "REGISTRATION",
    field: str = "STATUS",
    value="ACTIVE",
    state: str = "FOUND",
    rights: str = "PUBLIC",
    source_date: str = "2026-10-01",
) -> CompanySemanticFact:
    row = CompanySemanticFact(
        fact_ref=f"fact:{uuid4()}",
        item_ref=f"item:{uuid4()}",
        company_id=company_id,
        section_key=section,
        field_key=field,
        period_identity="",
        item_identity="",
        selected_evidence={
            "evidence_ref": f"evidence:{uuid4()}",
            "source_code": "OFFICIAL",
            "source_class": "OFFICIAL_PRIMARY",
            "value": value,
            "source_data_date": source_date,
            "retrieved_at": NOW.isoformat(),
            "rights": rights,
        },
        state=state,
        rights=rights,
        is_current=True,
    )
    session.add(row)
    session.flush()
    return row


def _set_fact(
    row: CompanySemanticFact,
    *,
    value,
    state: str = "FOUND",
    retrieved_at: datetime | None = None,
    source_date: str | None = None,
) -> None:
    evidence = dict(row.selected_evidence)
    evidence["value"] = value
    evidence["retrieved_at"] = (retrieved_at or NOW).isoformat()
    if source_date is not None:
        evidence["source_data_date"] = source_date
    row.selected_evidence = evidence
    row.state = state


def _seed(email: str, inn: str, *, entitlement: bool = True, save: bool = True):
    with Session(engine) as session:
        company = _company(session, inn)
        company_id = company.id
        session.commit()
    user_id, workspace_id = _bootstrap(email, f"Monitoring {inn}")
    with Session(engine) as session:
        if entitlement:
            _enable_entitlement(session, workspace_id)
        if save:
            save_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
        session.commit()
    return user_id, workspace_id, company_id


def _count(session: Session, model, company_id: int | None = None) -> int:
    stmt = sa.select(sa.func.count()).select_from(model)
    if company_id is not None and hasattr(model, "company_id"):
        stmt = stmt.where(model.company_id == company_id)
    return int(session.scalar(stmt) or 0)


def test_subscription_guards_idempotency_baseline_audit_and_atomicity():
    email = f"monitoring-guards-{uuid4()}@example.test"
    inn = "9799990001"
    try:
        user_id, workspace_id, company_id = _seed(email, inn, entitlement=False, save=False)
        with Session(engine) as session:
            with pytest.raises(ActionDenied, match="Функция"):
                subscribe_company(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            assert _count(session, MonitoringSubscription, company_id) == 0
            _enable_entitlement(session, workspace_id)
            with pytest.raises(ActionDenied) as missing_saved:
                subscribe_company(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            assert missing_saved.value.code == "saved_company_required"
            save_company(session, user_id=user_id, workspace_id=workspace_id, inn=inn)
            fact = _semantic_fact(session, company_id)
            subscription, created = subscribe_company(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                inn=inn,
                now=NOW,
            )
            assert created and subscription.status == "ACTIVE"
            assert subscription.baseline_snapshot_id is not None
            assert _count(session, MonitoringEvent, company_id) == 0
            assert _count(session, WorkspaceFeedEntry) == 0
            again, created = subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            assert not created and again.id == subscription.id
            assert _count(session, CompanyMonitoringSnapshot, company_id) == 1
            assert _count(session, MonitoringSubscription, company_id) == 1
            actions = tuple(
                session.scalars(
                    sa.select(WorkspaceAuditEvent.action).where(
                        WorkspaceAuditEvent.workspace_id == workspace_id,
                        WorkspaceAuditEvent.action == "monitoring.subscribe",
                    )
                ).all()
            )
            assert actions == ("monitoring.subscribe", "monitoring.subscribe")
            assert fact.selected_evidence["value"] == "ACTIVE"
            session.rollback()
        with Session(engine) as session:
            assert _count(session, MonitoringSubscription, company_id) == 0
            assert _count(session, CompanyMonitoringSnapshot, company_id) == 0
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkspaceAuditEvent)
                .where(
                    WorkspaceAuditEvent.workspace_id == workspace_id,
                    WorkspaceAuditEvent.action == "monitoring.subscribe",
                )
            ) == 0
    finally:
        _cleanup(email, inns=(inn,))


def test_subscription_permission_and_active_membership_required():
    email = f"monitoring-permission-{uuid4()}@example.test"
    inn = "7812345678"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            member_role_id = session.scalar(
                sa.select(WorkspaceRole.id).where(
                    WorkspaceRole.workspace_id == workspace_id,
                    WorkspaceRole.role_key == "MEMBER",
                )
            )
            membership = session.scalar(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_id,
                )
            )
            membership.role_id = member_role_id
            session.flush()
            with pytest.raises(ActionDenied) as denied:
                subscribe_company(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            assert denied.value.code == "permission_denied"
            membership.status = "suspended"
            session.flush()
            with pytest.raises(ActionDenied) as inactive:
                subscribe_company(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            assert inactive.value.code == "membership_required"
            assert _count(session, MonitoringSubscription, company_id) == 0
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))


def test_subscription_and_audit_fail_in_the_same_transaction(monkeypatch):
    email = f"monitoring-audit-atomic-{uuid4()}@example.test"
    inn = "7710140679"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        from workspace_app import monitoring_service

        def invalid_audit(session, **_kwargs):
            session.add(
                WorkspaceAuditEvent(
                    workspace_id=None,
                    actor_user_id=user_id,
                    action="monitoring.subscribe",
                    target_type="company",
                    target_ref=inn,
                    outcome="success",
                )
            )

        monkeypatch.setattr(monitoring_service, "_audit", invalid_audit)
        with Session(engine) as session:
            with pytest.raises(IntegrityError):
                subscribe_company(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            session.rollback()
        with Session(engine) as session:
            assert _count(session, MonitoringSubscription, company_id) == 0
            assert _count(session, CompanyMonitoringSnapshot, company_id) == 0
    finally:
        _cleanup(email, inns=(inn,))


def test_semantic_change_dedupe_timestamp_order_and_privacy():
    email = f"monitoring-diff-{uuid4()}@example.test"
    inn = "7801234567"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            public = _semantic_fact(
                session, company_id, value={"a": 1, "b": 2}
            )
            internal = _semantic_fact(
                session,
                company_id,
                section="INTERNAL",
                field="SECRET",
                value="secret-token-never-visible",
                rights="INTERNAL_ONLY",
            )
            subscription, _ = subscribe_company(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                inn=inn,
                now=NOW,
            )
            baseline = session.get(
                CompanyMonitoringSnapshot, subscription.baseline_snapshot_id
            )
            assert baseline.fact_count == 1
            assert "secret-token-never-visible" not in str(baseline.facts)
            _set_fact(
                public,
                value={"b": 2, "a": 1},
                retrieved_at=NOW + timedelta(hours=1),
            )
            _set_fact(internal, value="another-private-secret")
            session.flush()
            same = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=2)
            )
            assert same.detected_change_count == 0
            assert same.feed_entry_count == 0
            _set_fact(public, value={"a": 1, "b": 3})
            session.flush()
            changed = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=3)
            )
            assert changed.detected_change_count == 1
            assert changed.canonical_event_count == 1
            assert changed.feed_entry_count == 1
            event_row = session.scalar(
                sa.select(MonitoringEvent).where(MonitoringEvent.company_id == company_id)
            )
            assert event_row.origin == "SOURCE_CHANGE"
            assert event_row.old_value == {"a": 1, "b": 2}
            assert event_row.new_value == {"a": 1, "b": 3}
            assert event_row.severity == "HIGH"
            assert event_row.severity_policy_version
            assert "private" not in str(event_row.__dict__)
            repeat = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=4)
            )
            assert repeat.detected_change_count == 0
            assert repeat.feed_entry_count == 0
            assert _count(session, MonitoringEvent, company_id) == 1
            assert len(
                list_workspace_feed(
                    session, user_id=user_id, workspace_id=workspace_id
                )
            ) == 1
            session.commit()
    finally:
        _cleanup(email, inns=(inn,))


def test_state_and_missing_fact_changes_fail_closed():
    email = f"monitoring-state-{uuid4()}@example.test"
    inn = "7707083893"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            fact = _semantic_fact(session, company_id, value="DEBT", field="TAX_DEBT")
            subscription, _ = subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn, now=NOW
            )
            baseline = session.get(
                CompanyMonitoringSnapshot, subscription.baseline_snapshot_id
            )
            _set_fact(fact, value=None, state="SOURCE_UNAVAILABLE")
            session.flush()
            unavailable = capture_snapshot(
                session, company_id=company_id, captured_at=NOW + timedelta(hours=1)
            )
            changes = detect_changes(baseline, unavailable)
            assert len(changes) == 1
            assert changes[0].change_kind == "STATE_CHANGED"
            assert changes[0].origin == "COVERAGE_CHANGE"
            assert changes[0].user_visible is False
            result = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=2)
            )
            assert result.feed_entry_count == 0
            assert _count(session, MonitoringEvent, company_id) == 1
            fact.is_current = False
            session.flush()
            missing = capture_snapshot(
                session, company_id=company_id, captured_at=NOW + timedelta(hours=3)
            )
            removed = detect_changes(unavailable, missing)
            assert len(removed) == 1
            assert removed[0].change_kind == "FACT_REMOVED"
            assert removed[0].origin == "COVERAGE_CHANGE"
            assert removed[0].user_visible is False
            result = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=4)
            )
            assert result.feed_entry_count == 0
            assert _count(session, WorkspaceFeedEntry) == 0
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))


def test_found_not_found_is_visible_but_source_recovery_is_coverage():
    email = f"monitoring-state-visible-{uuid4()}@example.test"
    inn = "7712345678"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            fact = _semantic_fact(session, company_id, value="DEBT", field="TAX_DEBT")
            subscription, _ = subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            baseline = session.get(
                CompanyMonitoringSnapshot, subscription.baseline_snapshot_id
            )
            _set_fact(fact, value=None, state="NOT_FOUND")
            session.flush()
            resolved = capture_snapshot(session, company_id=company_id)
            changes = detect_changes(baseline, resolved)
            assert len(changes) == 1
            assert changes[0].origin == "SOURCE_CHANGE"
            assert changes[0].change_kind == "STATE_CHANGED"
            assert changes[0].old_value == "DEBT"
            assert changes[0].new_value is None
            assert changes[0].severity == "INFO"
            assert changes[0].user_visible is True
            first = monitor_company_once(session, company_id=company_id)
            assert first.feed_entry_count == 1
            _set_fact(fact, value=None, state="SOURCE_UNAVAILABLE")
            session.flush()
            second = monitor_company_once(session, company_id=company_id)
            assert second.feed_entry_count == 0
            _set_fact(fact, value="DEBT", state="FOUND")
            session.flush()
            third = monitor_company_once(session, company_id=company_id)
            assert third.feed_entry_count == 0
            assert _count(session, WorkspaceFeedEntry) == 1
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))


def test_two_workspaces_share_one_event_and_feed_is_tenant_scoped():
    email_a = f"monitoring-a-{uuid4()}@example.test"
    email_b = f"monitoring-b-{uuid4()}@example.test"
    inn = "7728168971"
    try:
        user_a, workspace_a, company_id = _seed(email_a, inn)
        user_b, workspace_b = _bootstrap(email_b, "Monitoring B")
        with Session(engine) as session:
            _enable_entitlement(session, workspace_b)
            save_company(
                session, user_id=user_b, workspace_id=workspace_b, inn=inn
            )
            fact = _semantic_fact(
                session, company_id, section="ADDRESS", field="ADDRESS", value="Old"
            )
            subscribe_company(
                session, user_id=user_a, workspace_id=workspace_a, inn=inn, now=NOW
            )
            subscribe_company(
                session, user_id=user_b, workspace_id=workspace_b, inn=inn, now=NOW
            )
            _set_fact(fact, value="New")
            session.flush()
            result = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=1)
            )
            assert result.subscription_count == 2
            assert result.canonical_event_count == 1
            assert result.feed_entry_count == 2
            assert _count(session, MonitoringEvent, company_id) == 1
            entries_a = list_workspace_feed(
                session, user_id=user_a, workspace_id=workspace_a
            )
            entries_b = list_workspace_feed(
                session, user_id=user_b, workspace_id=workspace_b
            )
            assert len(entries_a) == len(entries_b) == 1
            assert entries_a[0].entry_id != entries_b[0].entry_id
            assert entries_a[0].event_ref == entries_b[0].event_ref
            with pytest.raises(ActionDenied) as cross_tenant:
                mark_feed_entry_read(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    entry_id=entries_b[0].entry_id,
                )
            assert cross_tenant.value.code == "feed_entry_not_found"
            entry, changed = mark_feed_entry_read(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                entry_id=entries_a[0].entry_id,
            )
            assert changed and entry.read_at
            assert entries_b[0].read_at is None
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkspaceAuditEvent)
                .where(
                    WorkspaceAuditEvent.workspace_id == workspace_a,
                    WorkspaceAuditEvent.action == "monitoring.feed.read",
                )
            ) == 1
            session.commit()
        with Session(engine) as session:
            event_id = session.scalar(
                sa.select(MonitoringEvent.id).where(
                    MonitoringEvent.company_id == company_id
                )
            )
            with pytest.raises(IntegrityError):
                session.add(
                    WorkspaceFeedEntry(
                        workspace_id=workspace_a,
                        subscription_id=entries_b[0].subscription_id,
                        event_id=event_id,
                    )
                )
                session.flush()
            session.rollback()
    finally:
        _cleanup(email_a, email_b, inns=(inn,))


def test_pause_resume_uses_new_baseline_without_replay():
    email = f"monitoring-pause-{uuid4()}@example.test"
    inn = "7736050003"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            fact = _semantic_fact(session, company_id, value="A")
            subscription, _ = subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn, now=NOW
            )
            original_baseline_id = subscription.baseline_snapshot_id
            with pytest.raises(ActionDenied) as active_unsave:
                unsave_company(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            assert active_unsave.value.code == "monitoring_active"
            pause_subscription(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            assert unsave_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            with pytest.raises(ActionDenied) as missing_saved:
                resume_subscription(
                    session, user_id=user_id, workspace_id=workspace_id, inn=inn
                )
            assert missing_saved.value.code == "saved_company_required"
            save_company(session, user_id=user_id, workspace_id=workspace_id, inn=inn)
            _set_fact(fact, value="B")
            session.flush()
            while_paused = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=1)
            )
            assert while_paused.subscription_count == 0
            assert while_paused.feed_entry_count == 0
            assert _count(session, MonitoringEvent, company_id) == 0
            resumed, changed = resume_subscription(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                inn=inn,
                now=NOW + timedelta(hours=2),
            )
            assert changed and resumed.baseline_snapshot_id != original_baseline_id
            no_replay = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=3)
            )
            assert no_replay.detected_change_count == 0
            _set_fact(fact, value="C")
            session.flush()
            after_resume = monitor_company_once(
                session, company_id=company_id, detected_at=NOW + timedelta(hours=4)
            )
            assert after_resume.feed_entry_count == 1
            actions = set(
                session.scalars(
                    sa.select(WorkspaceAuditEvent.action).where(
                        WorkspaceAuditEvent.workspace_id == workspace_id,
                        WorkspaceAuditEvent.action.in_(
                            ("monitoring.pause", "monitoring.resume")
                        ),
                    )
                ).all()
            )
            assert actions == {"monitoring.pause", "monitoring.resume"}
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))


def test_monitoring_http_api_csrf_scope_and_feed():
    email = f"monitoring-http-{uuid4()}@example.test"
    inn = "7728168971"
    item = projection(sequence=100_100_151)
    inn = item.company.inn
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository((item,)),
                session_factory=SessionLocal,
            )
        )
        assert web.get(f"/app/api/companies/{inn}/monitoring").status_code == 401
        _login(web, email)
        state = web.get(f"/app/api/companies/{inn}/monitoring")
        assert state.status_code == 200
        assert state.json()["state"] == "NOT_ACTIVE"
        denied = web.post(f"/app/api/companies/{inn}/monitoring/enable")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "csrf_invalid"
        csrf = web.cookies[CSRF_COOKIE]
        enabled = web.post(
            f"/app/api/companies/{inn}/monitoring/enable",
            headers={"x-csrf-token": csrf},
        )
        assert enabled.status_code == 201
        assert enabled.json()["monitoring"]["state"] == "ACTIVE"
        repeat = web.post(
            f"/app/api/companies/{inn}/monitoring/enable",
            headers={"x-csrf-token": csrf},
        )
        assert repeat.status_code == 200
        assert repeat.json()["changed"] is False
        with Session(engine) as session:
            fact = _semantic_fact(
                session, company_id, section="ADDRESS", field="ADDRESS", value="Old"
            )
            session.commit()
        with Session(engine) as session:
            result = monitor_company_once(session, company_id=company_id)
            assert result.feed_entry_count == 1
            session.commit()
        feed = web.get("/app/api/monitoring")
        assert feed.status_code == 200
        assert len(feed.json()["items"]) == 1
        entry_id = feed.json()["items"][0]["id"]
        read = web.post(
            f"/app/api/monitoring/feed/{entry_id}/read",
            headers={"x-csrf-token": csrf},
        )
        assert read.status_code == 200
        assert read.json()["changed"] is True
        pause = web.post(
            f"/app/api/companies/{inn}/monitoring/pause",
            headers={"x-csrf-token": csrf},
        )
        assert pause.json()["monitoring"]["state"] == "PAUSED"
        with Session(engine) as session:
            assert _count(session, MonitoringSubscription, company_id) == 1
            assert _count(session, MonitoringEvent, company_id) == 1
    finally:
        _cleanup(email, inns=(inn,))
