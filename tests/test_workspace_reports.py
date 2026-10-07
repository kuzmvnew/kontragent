from __future__ import annotations

import csv
import io
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from app.models.workspace import (
    WorkspaceAuditEvent,
    WorkspaceEntitlement,
    WorkspaceReport,
    WorkspaceRole,
    WorkspaceRoleCapability,
)
from tests.public_test_support import projection
from tests.test_workspace_p0 import (
    FakePublicRepository,
    _bootstrap,
    _cleanup,
    _login,
)
from workspace_app.main import create_app
from workspace_app.report_service import (
    build_report_snapshot,
    canonical_json_bytes,
    generate_report,
    get_report,
    list_reports,
    report_csv_bytes,
    snapshot_sha256,
)
from workspace_app.service import ActionDenied


def _add_company(inn: str, name: str) -> int:
    with Session(engine) as session:
        company = Company(inn=inn, name=name, entity_type="legal")
        session.add(company)
        session.flush()
        company_id = company.id
        session.commit()
    return company_id


def _changed_projection(item):
    factor = item.risk.factors[0].model_copy(
        update={"title": "Изменённый семантический фактор"}
    )
    risk = item.risk.model_copy(update={"factors": (factor,)})
    return item.model_copy(update={"risk": risk})


def test_report_service_persists_historical_snapshot_hash_audit_and_tenant_scope():
    email_a = f"workspace-report-a-{uuid4()}@example.test"
    email_b = f"workspace-report-b-{uuid4()}@example.test"
    item = projection(sequence=100_300_101)
    inn = item.company.inn
    repository = FakePublicRepository((item,))
    try:
        company_id = _add_company(inn, item.company.name)
        user_a, workspace_a = _bootstrap(email_a, "Reports A")
        user_b, workspace_b = _bootstrap(email_b, "Reports B")
        with Session(engine) as session:
            report_a = generate_report(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                inn=inn,
                projection_repository=repository,
            )
            report_a_id = report_a.id
            snapshot_a = deepcopy(report_a.snapshot)
            hash_a = report_a.snapshot_sha256
            assert hash_a == snapshot_sha256(snapshot_a)
            assert report_a.company_id == company_id
            assert report_a.generated_by_user_id == user_a
            assert "company_id" not in canonical_json_bytes(snapshot_a).decode("utf-8")
            audit = session.scalar(
                sa.select(WorkspaceAuditEvent).where(
                    WorkspaceAuditEvent.workspace_id == workspace_a,
                    WorkspaceAuditEvent.action == "report.generate",
                    WorkspaceAuditEvent.target_ref == str(report_a_id),
                )
            )
            assert audit is not None and audit.outcome == "success"
            session.commit()

        repository.items[inn] = _changed_projection(item)
        with Session(engine) as session:
            persisted_a = get_report(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                report_id=report_a_id,
            )
            assert persisted_a.snapshot == snapshot_a
            assert persisted_a.snapshot_sha256 == hash_a
            report_b = generate_report(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                inn=inn,
                projection_repository=repository,
            )
            assert report_b.id != report_a_id
            assert report_b.snapshot != snapshot_a
            assert report_b.snapshot_sha256 != hash_a
            assert len(
                list_reports(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    limit=50,
                )
            ) == 2
            with pytest.raises(ActionDenied) as cross_tenant:
                get_report(
                    session,
                    user_id=user_b,
                    workspace_id=workspace_b,
                    report_id=report_a_id,
                )
            assert cross_tenant.value.code == "report_not_found"
            session.commit()

        with Session(engine) as session:
            report = session.get(WorkspaceReport, report_a_id)
            assert report is not None
            report.snapshot["subject"]["name"] = "tampered"
            with session.no_autoflush, pytest.raises(ActionDenied) as corrupt:
                get_report(
                    session,
                    user_id=user_a,
                    workspace_id=workspace_a,
                    report_id=report_a_id,
                )
            assert corrupt.value.code == "snapshot_integrity_failed"
            session.rollback()

        with Session(engine) as session:
            with pytest.raises(DBAPIError):
                session.execute(
                    sa.update(WorkspaceReport)
                    .where(WorkspaceReport.id == report_a_id)
                    .values(subject_name="mutation denied")
                )
            session.rollback()

        with Session(engine) as session:
            session.execute(sa.delete(Company).where(Company.id == company_id))
            session.commit()
        with Session(engine) as session:
            preserved = get_report(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                report_id=report_a_id,
            )
            assert preserved.company_id is None
            assert preserved.snapshot == snapshot_a
    finally:
        _cleanup(email_a, email_b, inns=(inn,))


def test_report_html_api_exports_entitlement_policy_rbac_and_cross_tenant():
    email_a = f"workspace-report-http-a-{uuid4()}@example.test"
    email_b = f"workspace-report-http-b-{uuid4()}@example.test"
    item = projection(sequence=100_300_102)
    inn = item.company.inn
    try:
        _add_company(inn, item.company.name)
        _user_a, workspace_a = _bootstrap(email_a, "Reports HTTP A")
        _user_b, workspace_b = _bootstrap(email_b, "Reports HTTP B")
        repository = FakePublicRepository((item,))
        web_a = TestClient(
            create_app(public_repository=repository, session_factory=SessionLocal)
        )
        _login(web_a, email_a)
        empty = web_a.get("/app/reports")
        assert empty.status_code == 200
        assert "Пока нет отчётов" in empty.text
        assert 'href="/app/search"' in empty.text

        card = web_a.get(f"/app/companies/{inn}")
        assert card.status_code == 200
        assert f'action="/app/companies/{inn}/reports"' in card.text
        assert "Сформировать отчёт" in card.text
        assert web_a.post(f"/app/companies/{inn}/reports").status_code == 403

        csrf = web_a.cookies.get("nextcompany_csrf")
        created = web_a.post(
            f"/app/companies/{inn}/reports",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert created.headers["location"].startswith("/app/reports/")
        report_id = created.headers["location"].rsplit("/", 1)[-1]
        detail = web_a.get(created.headers["location"])
        assert detail.status_code == 200
        assert "Данные зафиксированы на момент формирования отчёта" in detail.text
        assert item.company.name in detail.text
        history = web_a.get("/app/reports")
        assert report_id in history.text

        json_export = web_a.get(f"/app/reports/{report_id}/export.json")
        assert json_export.status_code == 200
        assert json_export.headers["content-type"] == "application/json"
        assert "attachment; filename=\"next-company-report-" in json_export.headers[
            "content-disposition"
        ]
        assert json_export.json()["report_id"] == report_id
        assert "company_id" not in json_export.text
        csv_export = web_a.get(f"/app/reports/{report_id}/export.csv")
        assert csv_export.status_code == 200
        assert csv_export.content.startswith(b"\xef\xbb\xbf")
        assert csv_export.headers["content-type"].startswith(
            "text/csv; charset=utf-8"
        )
        assert item.company.name in csv_export.content.decode("utf-8-sig")

        api_detail = web_a.get(f"/app/api/reports/{report_id}")
        api_json_export = web_a.get(
            f"/app/api/reports/{report_id}/export.json"
        )
        api_csv_export = web_a.get(f"/app/api/reports/{report_id}/export.csv")
        assert api_detail.content == json_export.content == api_json_export.content
        assert api_csv_export.content == csv_export.content
        assert web_a.get("/app/api/reports").json()["items"][0][
            "report_id"
        ] == report_id

        with Session(engine) as session:
            entitlement = session.scalar(
                sa.select(WorkspaceEntitlement).where(
                    WorkspaceEntitlement.workspace_id == workspace_a,
                    WorkspaceEntitlement.entitlement_key == "reports.enabled",
                )
            )
            entitlement.enabled = False
            session.commit()
        blocked = web_a.post(
            f"/app/api/companies/{inn}/reports",
            headers={"x-csrf-token": csrf},
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "entitlement_blocked"
        assert web_a.get(f"/app/reports/{report_id}").status_code == 200
        assert web_a.get(f"/app/reports/{report_id}/export.json").status_code == 200

        with Session(engine) as session:
            owner_role_id = session.scalar(
                sa.select(WorkspaceRole.id).where(
                    WorkspaceRole.workspace_id == workspace_a,
                    WorkspaceRole.role_key == "OWNER",
                )
            )
            session.execute(
                sa.delete(WorkspaceRoleCapability).where(
                    WorkspaceRoleCapability.role_id == owner_role_id,
                    WorkspaceRoleCapability.capability_key == "report.export",
                )
            )
            session.commit()
        denied_export = web_a.get(f"/app/api/reports/{report_id}/export.json")
        assert denied_export.status_code == 403
        assert denied_export.json()["error"]["code"] == "permission_denied"

        web_b = TestClient(
            create_app(public_repository=repository, session_factory=SessionLocal)
        )
        _login(web_b, email_b)
        assert web_b.get(f"/app/reports/{report_id}").status_code == 404
        assert web_b.get(f"/app/api/reports/{report_id}").status_code == 404
        assert web_b.get(f"/app/api/reports/{report_id}/export.json").status_code == 404
        assert web_b.get(f"/app/api/reports/{report_id}/export.csv").status_code == 404
        assert web_b.get("/app/api/reports").json()["items"] == []
        assert workspace_a != workspace_b
    finally:
        _cleanup(email_a, email_b, inns=(inn,))


def test_csv_export_is_deterministic_structured_utf8_and_formula_safe():
    item = projection(sequence=100_300_103)
    report_id = uuid4()
    snapshot, *_ = build_report_snapshot(
        report_id=report_id,
        generated_at=datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc),
        projection=item,
    )
    injected = deepcopy(snapshot)
    injected["sections"] = [
        {
            "section_key": "formula_safety",
            "title": "Формулы и структура",
            "state": "FOUND",
            "items": [
                {
                    "field_key": "equals",
                    "label": "Опасное значение",
                    "state": "FOUND",
                    "value": "=SUM(1,1)",
                    "period": None,
                    "source": {
                        "name": "+Источник",
                        "source_data_date": "2026-10-07",
                        "freshness": "CURRENT",
                    },
                    "limitations": ["@ограничение"],
                },
                {
                    "field_key": "structured",
                    "label": "Структура",
                    "state": "FOUND",
                    "value": {"б": 2, "а": [1, 2]},
                    "period": "-2026",
                    "source": {
                        "name": "ФНС",
                        "source_data_date": "2026-10-07",
                        "freshness": "CURRENT",
                    },
                    "limitations": [],
                },
            ],
        }
    ]
    first = report_csv_bytes(injected)
    second = report_csv_bytes(injected)
    assert first == second
    assert first.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(first.decode("utf-8-sig"))))
    assert rows[0]["value"] == "'=SUM(1,1)"
    assert rows[0]["source"] == "'+Источник"
    assert rows[0]["limitations"] == '["@ограничение"]'
    assert rows[1]["period"] == "'-2026"
    assert rows[1]["value"] == '{"а":[1,2],"б":2}'
    assert "Формулы и структура" in first.decode("utf-8-sig")
