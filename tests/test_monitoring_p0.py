from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import re
from threading import Barrier
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


def _set_fact_rights(
    row: CompanySemanticFact, *, rights: str, value, evidence_ref: str | None = None
) -> None:
    evidence = dict(row.selected_evidence)
    evidence["rights"] = rights
    evidence["value"] = value
    if evidence_ref is not None:
        evidence["evidence_ref"] = evidence_ref
        evidence["source_code"] = evidence_ref
    row.rights = rights
    row.selected_evidence = evidence


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
            assert "secret-token-never-visible" not in str(baseline.last_known_business_facts)
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


def test_database_rejects_history_updates_but_lifecycle_rows_remain_mutable():
    email = f"monitoring-immutable-{uuid4()}@example.test"
    inn = f"98{uuid4().int % 100_000_000:08d}"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            fact = _semantic_fact(session, company_id, value=100, field="TAX_DEBT")
            fact_ref = fact.fact_ref
            subscription, _ = subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            snapshot_id = subscription.baseline_snapshot_id
            _set_fact(fact, value=150)
            session.flush()
            result = monitor_company_once(session, company_id=company_id)
            assert result.feed_entry_count == 1
            event_id = session.scalar(
                sa.select(MonitoringEvent.id).where(MonitoringEvent.company_id == company_id)
            )
            entry_id = session.scalar(
                sa.select(WorkspaceFeedEntry.id).where(WorkspaceFeedEntry.workspace_id == workspace_id)
            )
            session.commit()

        with Session(engine) as session:
            snapshot = session.get(CompanyMonitoringSnapshot, snapshot_id)
            event_row = session.get(MonitoringEvent, event_id)
            original_facts = snapshot.facts
            original_fingerprint = snapshot.fingerprint
            original_old = event_row.old_value
            original_new = event_row.new_value

        for model, row_id, field, replacement in (
            (CompanyMonitoringSnapshot, snapshot_id, "fingerprint", "0" * 64),
            (MonitoringEvent, event_id, "new_value", "tampered"),
        ):
            with Session(engine) as session:
                setattr(session.get(model, row_id), field, replacement)
                with pytest.raises(ValueError, match="immutable"):
                    session.flush()
                session.rollback()

        for model, row_id, values in (
            (CompanyMonitoringSnapshot, snapshot_id, {"facts": []}),
            (MonitoringEvent, event_id, {"old_value": "tampered"}),
        ):
            with Session(engine) as session:
                with pytest.raises(sa.exc.DBAPIError, match="immutable"):
                    session.execute(sa.update(model).where(model.id == row_id).values(**values))
                session.rollback()
            with engine.begin() as connection:
                with pytest.raises(sa.exc.DBAPIError, match="immutable"):
                    connection.execute(sa.update(model).where(model.id == row_id).values(**values))
                # The PostgreSQL transaction is aborted by the trigger.
                connection.rollback()

        for table, row_id, column, value in (
            ("company_monitoring_snapshots", snapshot_id, "facts", "[]"),
            ("monitoring_events", event_id, "new_value", '"tampered"'),
        ):
            with Session(engine) as session:
                with pytest.raises(sa.exc.DBAPIError, match="immutable"):
                    session.execute(
                        sa.text(
                            f"UPDATE {table} SET {column} = CAST(:value AS jsonb) WHERE id = :id"
                        ),
                        {"value": value, "id": row_id},
                    )
                session.rollback()

        with Session(engine) as session:
            snapshot = session.get(CompanyMonitoringSnapshot, snapshot_id)
            event_row = session.get(MonitoringEvent, event_id)
            assert snapshot.facts == original_facts
            assert snapshot.fingerprint == original_fingerprint
            assert event_row.old_value == original_old
            assert event_row.new_value == original_new
            pause_subscription(session, user_id=user_id, workspace_id=workspace_id, inn=inn)
            resumed, changed = resume_subscription(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            assert changed and resumed.status == "ACTIVE"
            assert resumed.baseline_snapshot_id != snapshot_id
            read_entry, changed = mark_feed_entry_read(
                session, user_id=user_id, workspace_id=workspace_id, entry_id=entry_id
            )
            assert changed and read_entry.read_at is not None
            current_fact = session.get(CompanySemanticFact, fact_ref)
            _set_fact(current_fact, value=175)
            session.flush()
            scan = monitor_company_once(session, company_id=company_id)
            assert scan.feed_entry_count == 1
            assert resumed.last_checked_at is not None
            session.commit()
    finally:
        _cleanup(email, inns=(inn,))


def _exercise_recovery(
    *, gap_kind: str, recovery_value: int, multi_workspace: bool
) -> None:
    email_a = f"monitoring-recovery-a-{uuid4()}@example.test"
    email_b = f"monitoring-recovery-b-{uuid4()}@example.test" if multi_workspace else None
    inn = f"98{uuid4().int % 100_000_000:08d}"
    try:
        user_a, workspace_a, company_id = _seed(email_a, inn)
        user_b = workspace_b = None
        if multi_workspace:
            user_b, workspace_b = _bootstrap(email_b, "Monitoring recovery B")
        with Session(engine) as session:
            if multi_workspace:
                _enable_entitlement(session, workspace_b)
                save_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            fact = _semantic_fact(session, company_id, value=100, field="TAX_DEBT")
            subscription_a, _ = subscribe_company(
                session, user_id=user_a, workspace_id=workspace_a, inn=inn
            )
            if multi_workspace:
                subscribe_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            baseline = session.get(
                CompanyMonitoringSnapshot, subscription_a.baseline_snapshot_id
            )
            assert baseline.last_known_business_facts[0]["value"] == 100
            if gap_kind == "MISSING":
                fact.is_current = False
            else:
                _set_fact(fact, value=None, state="SOURCE_UNAVAILABLE")
            session.flush()
            gap = monitor_company_once(session, company_id=company_id)
            assert gap.feed_entry_count == 0
            gap_snapshot = session.get(
                CompanyMonitoringSnapshot, subscription_a.baseline_snapshot_id
            )
            assert gap_snapshot.fingerprint != baseline.fingerprint
            assert gap_snapshot.last_known_business_facts[0]["value"] == 100
            if gap_kind == "MISSING":
                assert gap_snapshot.facts == []
                fact.is_current = True
            _set_fact(fact, value=recovery_value, state="FOUND")
            session.flush()
            recovery = monitor_company_once(session, company_id=company_id)
            changed = recovery_value == 150
            assert recovery.canonical_event_count == int(changed)
            assert recovery.feed_entry_count == (2 if multi_workspace else 1) * int(changed)
            business_events = session.scalars(
                sa.select(MonitoringEvent).where(
                    MonitoringEvent.company_id == company_id,
                    MonitoringEvent.origin == "SOURCE_CHANGE",
                )
            ).all()
            assert len(business_events) == int(changed)
            if changed:
                assert business_events[0].change_kind == "FACT_CHANGED"
                assert business_events[0].old_value == 100
                assert business_events[0].new_value == 150
            feed_a = list_workspace_feed(
                session, user_id=user_a, workspace_id=workspace_a
            )
            assert len(feed_a) == int(changed)
            if changed:
                assert feed_a[0].old_value == 100
                assert feed_a[0].new_value == 150
            if multi_workspace:
                feed_b = list_workspace_feed(
                    session, user_id=user_b, workspace_id=workspace_b
                )
                assert len(feed_b) == int(changed)
                if changed:
                    assert feed_a[0].event_ref == feed_b[0].event_ref
                    with pytest.raises(ActionDenied, match="не найдено"):
                        mark_feed_entry_read(
                            session,
                            user_id=user_a,
                            workspace_id=workspace_a,
                            entry_id=feed_b[0].entry_id,
                        )
            repeat = monitor_company_once(session, company_id=company_id)
            assert repeat.detected_change_count == 0
            assert repeat.feed_entry_count == 0
            session.commit()
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository(()),
                session_factory=SessionLocal,
            )
        )
        _login(web, email_a)
        api_feed = web.get("/app/api/monitoring")
        assert api_feed.status_code == 200
        assert len(api_feed.json()["items"]) == int(changed)
        if changed:
            assert api_feed.json()["items"][0]["old_value"] == 100
            assert api_feed.json()["items"][0]["new_value"] == 150
    finally:
        _cleanup(email_a, *((email_b,) if email_b else ()), inns=(inn,))


@pytest.mark.parametrize("gap_kind", ("MISSING", "SOURCE_UNAVAILABLE"))
@pytest.mark.parametrize("recovery_value", (100, 150))
def test_last_known_business_state_survives_coverage_gap(
    gap_kind: str, recovery_value: int
):
    _exercise_recovery(
        gap_kind=gap_kind, recovery_value=recovery_value, multi_workspace=False
    )


@pytest.mark.parametrize("recovery_value", (100, 150))
def test_two_workspaces_share_recovery_verdict(recovery_value: int):
    _exercise_recovery(
        gap_kind="MISSING", recovery_value=recovery_value, multi_workspace=True
    )


def test_genuinely_new_business_fact_remains_fact_added():
    email = f"monitoring-new-fact-{uuid4()}@example.test"
    inn = f"98{uuid4().int % 100_000_000:08d}"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            subscribe_company(session, user_id=user_id, workspace_id=workspace_id, inn=inn)
            _semantic_fact(session, company_id, value=100, field="TAX_DEBT")
            result = monitor_company_once(session, company_id=company_id)
            assert result.canonical_event_count == 1
            assert result.feed_entry_count == 1
            event_row = session.scalar(
                sa.select(MonitoringEvent).where(MonitoringEvent.company_id == company_id)
            )
            assert event_row.origin == "SOURCE_CHANGE"
            assert event_row.change_kind == "FACT_ADDED"
            assert event_row.old_value is None
            assert event_row.new_value == 100
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))


def test_snapshot_fingerprint_includes_last_known_business_state():
    email = f"monitoring-fingerprint-{uuid4()}@example.test"
    inn = f"98{uuid4().int % 100_000_000:08d}"
    try:
        _user_id, _workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            fact = _semantic_fact(session, company_id, value=100, field="TAX_DEBT")
            known_100 = capture_snapshot(session, company_id=company_id)
            _set_fact(fact, value=150)
            session.flush()
            known_150 = capture_snapshot(session, company_id=company_id)
            fact.is_current = False
            session.flush()
            gap_100 = capture_snapshot(
                session, company_id=company_id, previous_snapshot=known_100
            )
            gap_150 = capture_snapshot(
                session, company_id=company_id, previous_snapshot=known_150
            )
            assert gap_100.facts == gap_150.facts == []
            assert gap_100.last_known_business_facts[0]["value"] == 100
            assert gap_150.last_known_business_facts[0]["value"] == 150
            assert gap_100.fingerprint != gap_150.fingerprint
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))


def test_concurrent_scans_keep_one_business_event_and_consistent_cursors():
    email_a = f"monitoring-concurrent-a-{uuid4()}@example.test"
    email_b = f"monitoring-concurrent-b-{uuid4()}@example.test"
    inn = f"98{uuid4().int % 100_000_000:08d}"
    try:
        user_a, workspace_a, company_id = _seed(email_a, inn)
        user_b, workspace_b = _bootstrap(email_b, "Monitoring concurrent B")
        with Session(engine) as session:
            _enable_entitlement(session, workspace_b)
            save_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            fact = _semantic_fact(session, company_id, value=100, field="TAX_DEBT")
            fact_ref = fact.fact_ref
            subscribe_company(session, user_id=user_a, workspace_id=workspace_a, inn=inn)
            subscribe_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            session.commit()
        with Session(engine) as session:
            fact = session.get(CompanySemanticFact, fact_ref)
            _set_fact(fact, value=150)
            session.commit()

        gate = Barrier(2)

        def scan():
            with Session(engine) as session:
                gate.wait(timeout=10)
                result = monitor_company_once(session, company_id=company_id)
                session.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _index: scan(), range(2)))
        assert sum(result.canonical_event_count for result in results) == 1
        assert sum(result.feed_entry_count for result in results) == 2
        with Session(engine) as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(MonitoringEvent).where(
                    MonitoringEvent.company_id == company_id,
                    MonitoringEvent.origin == "SOURCE_CHANGE",
                )
            ) == 1
            assert session.scalar(
                sa.select(sa.func.count()).select_from(WorkspaceFeedEntry).join(
                    MonitoringSubscription,
                    MonitoringSubscription.id == WorkspaceFeedEntry.subscription_id,
                ).where(MonitoringSubscription.company_id == company_id)
            ) == 2
            subscriptions = session.scalars(
                sa.select(MonitoringSubscription).where(
                    MonitoringSubscription.company_id == company_id
                )
            ).all()
            assert len(subscriptions) == 2
            for subscription in subscriptions:
                snapshot = session.get(
                    CompanyMonitoringSnapshot, subscription.baseline_snapshot_id
                )
                assert snapshot.facts[0]["value"] == 150
                assert snapshot.last_known_business_facts[0]["value"] == 150
    finally:
        _cleanup(email_a, email_b, inns=(inn,))


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
            # Recovery to the last authoritative NOT_FOUND state is coverage
            # recovery, not a new business change.
            _set_fact(fact, value=None, state="NOT_FOUND")
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


def _exercise_privacy_barrier(
    *, initial_rights: str, restored_value: int, multi_workspace: bool
) -> None:
    email_a = f"monitoring-privacy-a-{uuid4()}@example.test"
    email_b = f"monitoring-privacy-b-{uuid4()}@example.test" if multi_workspace else None
    item = projection(sequence=100_250_000 + uuid4().int % 100_000)
    inn = item.company.inn
    secret_evidence_1 = f"restricted-evidence-{uuid4()}"
    secret_evidence_2 = f"restricted-evidence-{uuid4()}"
    try:
        user_a, workspace_a, company_id = _seed(email_a, inn)
        user_b = workspace_b = None
        if multi_workspace:
            user_b, workspace_b = _bootstrap(email_b, "Monitoring privacy B")
        with Session(engine) as session:
            if multi_workspace:
                _enable_entitlement(session, workspace_b)
                save_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            fact = _semantic_fact(
                session, company_id, value=100, field="TAX_DEBT", rights=initial_rights
            )
            fact_ref = fact.fact_ref
            subscription_a, _ = subscribe_company(
                session, user_id=user_a, workspace_id=workspace_a, inn=inn
            )
            subscription_a_id = subscription_a.id
            if multi_workspace:
                subscribe_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            baseline_id = subscription_a.baseline_snapshot_id
            baseline = session.get(CompanyMonitoringSnapshot, baseline_id)
            baseline_fingerprint = baseline.fingerprint
            assert baseline.facts[0]["value"] == 100
            assert baseline.last_known_business_facts[0]["rights"] == initial_rights
            _set_fact_rights(
                fact,
                rights="INTERNAL_ONLY",
                value=100,
                evidence_ref=secret_evidence_1,
            )
            session.flush()
            downgrade = monitor_company_once(session, company_id=company_id)
            assert downgrade.canonical_event_count == 0
            assert downgrade.feed_entry_count == 0
            restricted = session.get(
                CompanyMonitoringSnapshot, subscription_a.baseline_snapshot_id
            )
            assert restricted.facts == []
            assert restricted.last_known_business_facts == []
            assert len(restricted.privacy_blocked_coordinates) == 1
            assert re.fullmatch(r"[0-9a-f]{64}", restricted.privacy_blocked_coordinates[0])
            restricted_barrier = list(restricted.privacy_blocked_coordinates)
            assert secret_evidence_1 not in str(restricted.__dict__)
            assert restricted.fingerprint != baseline_fingerprint
            restricted_fingerprint = restricted.fingerprint
            assert _count(session, MonitoringEvent, company_id) == 0
            session.commit()

        web = TestClient(
            create_app(
                public_repository=FakePublicRepository((item,)),
                session_factory=SessionLocal,
            )
        )
        _login(web, email_a)
        for path in (
            f"/app/api/companies/{inn}/monitoring",
            "/app/api/monitoring",
            f"/app/companies/{inn}/monitoring",
            f"/app/companies/{inn}",
            "/app/monitoring",
        ):
            response = web.get(path)
            assert response.status_code == 200
            assert secret_evidence_1 not in response.text
            assert "privacy_blocked_coordinates" not in response.text
        assert web.get("/app/api/monitoring").json()["items"] == []

        with Session(engine) as session:
            fact = session.get(CompanySemanticFact, fact_ref)
            subscription_a = session.get(MonitoringSubscription, subscription_a_id)
            _set_fact_rights(
                fact,
                rights="INTERNAL_ONLY",
                value=150,
                evidence_ref=secret_evidence_2,
            )
            session.flush()
            restricted_change = monitor_company_once(session, company_id=company_id)
            assert restricted_change.canonical_event_count == 0
            assert restricted_change.feed_entry_count == 0
            restricted_again = session.get(
                CompanyMonitoringSnapshot, subscription_a.baseline_snapshot_id
            )
            assert restricted_again.facts == []
            assert restricted_again.last_known_business_facts == []
            assert restricted_again.privacy_blocked_coordinates == restricted_barrier
            assert restricted_again.fingerprint == restricted_fingerprint
            assert secret_evidence_2 not in str(restricted_again.__dict__)

            fact.is_current = False
            session.flush()
            missing = monitor_company_once(session, company_id=company_id)
            assert missing.canonical_event_count == 0
            missing_snapshot = session.get(
                CompanyMonitoringSnapshot, subscription_a.baseline_snapshot_id
            )
            assert missing_snapshot.facts == []
            assert missing_snapshot.last_known_business_facts == []
            assert missing_snapshot.privacy_blocked_coordinates == restricted_barrier
            assert missing_snapshot.fingerprint == restricted_fingerprint

            fact.is_current = True
            _set_fact_rights(
                fact,
                rights=initial_rights,
                value=restored_value,
                evidence_ref=f"restored-evidence-{uuid4()}",
            )
            session.flush()
            restoration = monitor_company_once(session, company_id=company_id)
            assert restoration.canonical_event_count == 0
            assert restoration.feed_entry_count == 0
            restored = session.get(
                CompanyMonitoringSnapshot, subscription_a.baseline_snapshot_id
            )
            assert restored.privacy_blocked_coordinates == []
            assert restored.facts[0]["value"] == restored_value
            assert restored.last_known_business_facts[0]["value"] == restored_value
            assert restored.last_known_business_facts[0]["rights"] == initial_rights
            assert restored.fingerprint != restricted_fingerprint
            assert _count(session, MonitoringEvent, company_id) == 0

            future_value = 150 if restored_value == 100 else 175
            _set_fact(fact, value=future_value)
            session.flush()
            future = monitor_company_once(session, company_id=company_id)
            assert future.canonical_event_count == 1
            assert future.feed_entry_count == (2 if multi_workspace else 1)
            event_row = session.scalar(
                sa.select(MonitoringEvent).where(MonitoringEvent.company_id == company_id)
            )
            assert event_row.origin == "SOURCE_CHANGE"
            assert event_row.change_kind == "FACT_CHANGED"
            assert event_row.old_value == restored_value
            assert event_row.new_value == future_value
            assert secret_evidence_1 not in str(event_row.__dict__)
            assert secret_evidence_2 not in str(event_row.__dict__)
            repeat = monitor_company_once(session, company_id=company_id)
            assert repeat.feed_entry_count == 0
            if multi_workspace:
                feed_a = list_workspace_feed(
                    session, user_id=user_a, workspace_id=workspace_a
                )
                feed_b = list_workspace_feed(
                    session, user_id=user_b, workspace_id=workspace_b
                )
                assert len(feed_a) == len(feed_b) == 1
                assert feed_a[0].event_ref == feed_b[0].event_ref
                with pytest.raises(ActionDenied) as denied:
                    mark_feed_entry_read(
                        session,
                        user_id=user_a,
                        workspace_id=workspace_a,
                        entry_id=feed_b[0].entry_id,
                    )
                assert denied.value.code == "feed_entry_not_found"
            audit_rows = session.scalars(
                sa.select(WorkspaceAuditEvent).where(
                    WorkspaceAuditEvent.workspace_id == workspace_a
                )
            ).all()
            assert all(secret_evidence_1 not in str(row.__dict__) for row in audit_rows)
            assert all(secret_evidence_2 not in str(row.__dict__) for row in audit_rows)
            old_baseline = session.get(CompanyMonitoringSnapshot, baseline_id)
            assert old_baseline.facts[0]["value"] == 100
            assert old_baseline.fingerprint == baseline_fingerprint
            session.commit()

        api_feed = web.get("/app/api/monitoring")
        assert api_feed.status_code == 200
        assert len(api_feed.json()["items"]) == 1
        assert api_feed.json()["items"][0]["old_value"] == restored_value
        assert api_feed.json()["items"][0]["new_value"] == future_value
        for path in ("/app/api/monitoring", "/app/monitoring"):
            response = web.get(path)
            assert secret_evidence_1 not in response.text
            assert secret_evidence_2 not in response.text
    finally:
        _cleanup(email_a, *((email_b,) if email_b else ()), inns=(inn,))


@pytest.mark.parametrize("initial_rights", ("PUBLIC", "AUTHENTICATED_ONLY"))
@pytest.mark.parametrize("restored_value", (100, 150))
def test_privacy_barrier_redacts_restricted_state_and_resets_on_restoration(
    initial_rights: str, restored_value: int
):
    _exercise_privacy_barrier(
        initial_rights=initial_rights,
        restored_value=restored_value,
        multi_workspace=False,
    )


def test_two_workspaces_share_privacy_barrier_without_leaking_feed():
    _exercise_privacy_barrier(
        initial_rights="PUBLIC", restored_value=100, multi_workspace=True
    )


def test_public_and_authenticated_rights_are_both_monitoring_eligible():
    email = f"monitoring-eligible-rights-{uuid4()}@example.test"
    inn = f"98{uuid4().int % 100_000_000:08d}"
    try:
        user_id, workspace_id, company_id = _seed(email, inn)
        with Session(engine) as session:
            fact = _semantic_fact(session, company_id, value=100, rights="PUBLIC")
            subscription, _ = subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn
            )
            for rights in ("AUTHENTICATED_ONLY", "PUBLIC"):
                _set_fact_rights(fact, rights=rights, value=100)
                session.flush()
                result = monitor_company_once(session, company_id=company_id)
                assert result.canonical_event_count == 0
                assert result.feed_entry_count == 0
                snapshot = session.get(
                    CompanyMonitoringSnapshot, subscription.baseline_snapshot_id
                )
                assert snapshot.privacy_blocked_coordinates == []
                assert snapshot.facts[0]["rights"] == rights
                assert snapshot.last_known_business_facts[0]["rights"] == rights
            assert _count(session, MonitoringEvent, company_id) == 0
            session.rollback()
    finally:
        _cleanup(email, inns=(inn,))
