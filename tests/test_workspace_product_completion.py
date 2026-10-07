from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from app.models.workspace import WorkspaceAuditEvent
from tests.public_test_support import projection
from tests.test_monitoring_p0 import (
    NOW,
    _enable_entitlement,
    _semantic_fact,
    _set_fact,
)
from tests.test_workspace_p0 import (
    FakePublicRepository,
    _bootstrap,
    _cleanup,
    _login,
)
from workspace_app.dashboard_service import get_workspace_dashboard
from workspace_app.main import create_app
from workspace_app.monitoring_service import (
    get_workspace_subscription,
    list_workspace_feed,
    list_workspace_subscriptions,
    mark_feed_entry_read,
    monitor_company_once,
    pause_subscription,
    resume_subscription,
    subscribe_company,
)
from workspace_app.service import (
    ActionDenied,
    save_company,
    saved_companies,
    update_saved_company_note,
)


def _add_company(inn: str, name: str) -> int:
    with Session(engine) as session:
        company = Company(inn=inn, name=name, entity_type="legal")
        session.add(company)
        session.flush()
        company_id = company.id
        session.commit()
    return company_id


def test_dashboard_metrics_quota_recent_activity_and_persisted_read_state():
    email = f"workspace-dashboard-{uuid4()}@example.test"
    inn_active = f"91{uuid4().int % 100_000_000:08d}"
    inn_paused = f"92{uuid4().int % 100_000_000:08d}"
    try:
        company_active = _add_company(inn_active, "Dashboard Active")
        _add_company(inn_paused, "Dashboard Paused")
        user_id, workspace_id = _bootstrap(email, "Dashboard Metrics", saved_limit=4)
        with Session(engine) as session:
            _enable_entitlement(session, workspace_id)
            save_company(session, user_id=user_id, workspace_id=workspace_id, inn=inn_active)
            save_company(session, user_id=user_id, workspace_id=workspace_id, inn=inn_paused)
            fact = _semantic_fact(
                session,
                company_active,
                section="ADDRESS",
                field="ADDRESS",
                value="Before",
            )
            subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn_active
            )
            subscribe_company(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn_paused
            )
            pause_subscription(
                session, user_id=user_id, workspace_id=workspace_id, inn=inn_paused
            )
            _set_fact(fact, value="After")
            session.flush()
            monitor_company_once(
                session,
                company_id=company_active,
                detected_at=NOW + timedelta(hours=1),
            )
            dashboard = get_workspace_dashboard(
                session, user_id=user_id, workspace_id=workspace_id
            )
            assert dashboard.metrics.saved_companies_count == 2
            assert dashboard.metrics.saved_companies_limit == 4
            assert dashboard.metrics.saved_companies_remaining == 2
            assert dashboard.metrics.active_monitoring_count == 1
            assert dashboard.metrics.paused_monitoring_count == 1
            assert dashboard.metrics.unread_monitoring_event_count == 1
            assert dashboard.metrics.total_monitoring_event_count == 1
            assert {item.inn for item in dashboard.recent_saved} == {
                inn_active,
                inn_paused,
            }
            assert dashboard.recent_events[0].inn == inn_active
            entry_id = dashboard.recent_events[0].entry_id
            mark_feed_entry_read(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                entry_id=entry_id,
            )
            session.flush()
            refreshed = get_workspace_dashboard(
                session, user_id=user_id, workspace_id=workspace_id
            )
            assert refreshed.metrics.unread_monitoring_event_count == 0
            assert refreshed.recent_events[0].read_at is not None
            session.commit()
    finally:
        _cleanup(email, inns=(inn_active, inn_paused))


def test_saved_filter_note_audit_monitoring_state_and_cross_tenant_denial():
    email_a = f"workspace-saved-a-{uuid4()}@example.test"
    email_b = f"workspace-saved-b-{uuid4()}@example.test"
    inn = f"93{uuid4().int % 100_000_000:08d}"
    try:
        company_id = _add_company(inn, "Alpha Product Company")
        user_a, workspace_a = _bootstrap(email_a, "Saved Tenant A", saved_limit=3)
        user_b, workspace_b = _bootstrap(email_b, "Saved Tenant B", saved_limit=3)
        with Session(engine) as session:
            _enable_entitlement(session, workspace_a)
            save_company(session, user_id=user_a, workspace_id=workspace_a, inn=inn)
            save_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            _semantic_fact(session, company_id, value="ACTIVE")
            subscribe_company(
                session, user_id=user_a, workspace_id=workspace_a, inn=inn
            )
            saved_a = saved_companies(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                query="alpha product",
            )
            assert len(saved_a) == 1
            assert saved_a[0].monitoring_state == "ACTIVE"
            assert saved_companies(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                query=inn[2:8],
            )
            assert saved_companies(
                session,
                user_id=user_b,
                workspace_id=workspace_b,
            )[0].monitoring_state == "NOT_ENABLED"

            updated, changed = update_saved_company_note(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                saved_company_id=saved_a[0].saved_company_id,
                note="  Проверить\n договор   до пятницы  ",
            )
            assert changed is True
            assert updated.note == "Проверить договор до пятницы"
            with pytest.raises(ActionDenied) as cross_note:
                update_saved_company_note(
                    session,
                    user_id=user_b,
                    workspace_id=workspace_b,
                    saved_company_id=saved_a[0].saved_company_id,
                    note="cross tenant",
                )
            assert cross_note.value.code == "saved_company_not_found"
            with pytest.raises(ActionDenied) as too_long:
                update_saved_company_note(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    saved_company_id=saved_a[0].saved_company_id,
                    note="x" * 2_001,
                )
            assert too_long.value.code == "saved_note_too_long"
            cleared, changed = update_saved_company_note(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                saved_company_id=saved_a[0].saved_company_id,
                note=" \n ",
            )
            assert changed is True
            assert cleared.note is None
            actions = tuple(
                session.scalars(
                    sa.select(WorkspaceAuditEvent.action).where(
                        WorkspaceAuditEvent.workspace_id == workspace_a,
                        WorkspaceAuditEvent.action.in_(
                            ("company.saved_note.update", "company.saved_note.clear")
                        ),
                    ).order_by(WorkspaceAuditEvent.created_at, WorkspaceAuditEvent.id)
                ).all()
            )
            assert set(actions) == {
                "company.saved_note.update",
                "company.saved_note.clear",
            }
            session.commit()
    finally:
        _cleanup(email_a, email_b, inns=(inn,))


def test_monitoring_overview_subscription_list_feed_filters_and_tenant_scope():
    email_a = f"workspace-overview-a-{uuid4()}@example.test"
    email_b = f"workspace-overview-b-{uuid4()}@example.test"
    inn = f"94{uuid4().int % 100_000_000:08d}"
    try:
        company_id = _add_company(inn, "Overview Company")
        user_a, workspace_a = _bootstrap(email_a, "Overview Tenant A")
        user_b, workspace_b = _bootstrap(email_b, "Overview Tenant B")
        with Session(engine) as session:
            _enable_entitlement(session, workspace_a)
            _enable_entitlement(session, workspace_b)
            save_company(session, user_id=user_a, workspace_id=workspace_a, inn=inn)
            save_company(session, user_id=user_b, workspace_id=workspace_b, inn=inn)
            fact = _semantic_fact(
                session,
                company_id,
                section="ADDRESS",
                field="ADDRESS",
                value="One",
            )
            subscription, _created = subscribe_company(
                session, user_id=user_a, workspace_id=workspace_a, inn=inn
            )
            _set_fact(fact, value="Two")
            session.flush()
            monitor_company_once(
                session,
                company_id=company_id,
                detected_at=NOW + timedelta(hours=1),
            )
            _set_fact(fact, value="Three")
            session.flush()
            monitor_company_once(
                session,
                company_id=company_id,
                detected_at=NOW + timedelta(hours=2),
            )
            all_entries = list_workspace_feed(
                session, user_id=user_a, workspace_id=workspace_a
            )
            assert len(all_entries) == 2
            with pytest.raises(ActionDenied) as cross_feed:
                mark_feed_entry_read(
                    session,
                    user_id=user_b,
                    workspace_id=workspace_b,
                    entry_id=all_entries[0].entry_id,
                )
            assert cross_feed.value.code == "feed_entry_not_found"
            mark_feed_entry_read(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                entry_id=all_entries[-1].entry_id,
            )
            assert len(
                list_workspace_feed(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    read_state="unread",
                )
            ) == 1
            assert len(
                list_workspace_feed(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    read_state="read",
                )
            ) == 1
            assert len(
                list_workspace_feed(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    severity="LOW",
                )
            ) == 2
            listed = list_workspace_subscriptions(
                session, user_id=user_a, workspace_id=workspace_a
            )
            assert len(listed) == 1
            assert listed[0].latest_event_at == NOW + timedelta(hours=2)
            assert list_workspace_subscriptions(
                session, user_id=user_b, workspace_id=workspace_b
            ) == ()
            with pytest.raises(ActionDenied) as cross_subscription:
                get_workspace_subscription(
                    session,
                    user_id=user_b,
                    workspace_id=workspace_b,
                    subscription_id=subscription.id,
                )
            assert cross_subscription.value.code == "monitoring_subscription_not_found"
            with pytest.raises(ActionDenied) as cross_pause:
                pause_subscription(
                    session, user_id=user_b, workspace_id=workspace_b, inn=inn
                )
            assert cross_pause.value.code == "monitoring_not_active"
            with pytest.raises(ActionDenied) as cross_resume:
                resume_subscription(
                    session, user_id=user_b, workspace_id=workspace_b, inn=inn
                )
            assert cross_resume.value.code == "monitoring_not_active"
            with pytest.raises(ActionDenied) as invalid_filter:
                list_workspace_feed(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    read_state="other",
                )
            assert invalid_filter.value.code == "monitoring_filter_invalid"
            session.commit()
    finally:
        _cleanup(email_a, email_b, inns=(inn,))


def test_html_note_csrf_and_api_html_saved_filter_parity():
    email = f"workspace-note-html-{uuid4()}@example.test"
    item = projection(sequence=100_100_181)
    try:
        _add_company(item.company.inn, item.company.name)
        _bootstrap(email, "Note HTML")
        web = TestClient(
            create_app(
                public_repository=FakePublicRepository((item,)),
                session_factory=SessionLocal,
            )
        )
        _login(web, email)
        csrf = web.cookies.get("nextcompany_csrf")
        web.post(
            f"/app/api/companies/{item.company.inn}/saved",
            headers={"x-csrf-token": csrf},
        )
        listing = web.get(f"/app/api/saved-companies?q={item.company.inn}").json()
        saved_id = listing["items"][0]["id"]
        rejected = web.post(
            f"/app/saved/{saved_id}/note",
            data={"note": "No CSRF"},
        )
        assert rejected.status_code == 403
        updated = web.post(
            f"/app/saved/{saved_id}/note",
            data={"csrf": csrf, "note": "  HTML   note "},
            follow_redirects=False,
        )
        assert updated.status_code == 303
        html = web.get(f"/app/saved?q={item.company.inn}")
        api = web.get(f"/app/api/saved-companies?q={item.company.inn}")
        assert "HTML note" in html.text
        assert api.json()["items"][0]["note"] == "HTML note"
        assert api.json()["items"][0]["monitoring"]["state"] == "NOT_ENABLED"
    finally:
        _cleanup(email, inns=(item.company.inn,))
