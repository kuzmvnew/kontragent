from __future__ import annotations

import inspect

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select

import main
from app.aggregators import company_aggregator
from app.database.postgres import engine, get_session
from app.models.company import Company


client = TestClient(main.app)


def test_unknown_public_company_get_is_read_only_and_never_calls_provider(monkeypatch):
    unknown_inn = "0000000000"
    monkeypatch.setattr(
        company_aggregator,
        "fetch_external_sources",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external provider called")),
    )
    writes: list[str] = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(verb)

    with get_session() as session:
        before = session.scalar(select(func.count()).select_from(Company))
    event.listen(engine, "before_cursor_execute", observe)
    try:
        html_response = client.get(f"/company/{unknown_inn}")
        api_response = client.get(f"/api/company/{unknown_inn}")
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    with get_session() as session:
        after = session.scalar(select(func.count()).select_from(Company))

    assert html_response.status_code == 404
    assert api_response.status_code == 404
    assert before == after
    assert writes == []


def test_existing_public_company_get_has_zero_database_writes(monkeypatch):
    inn = "9900000199"
    with get_session() as session:
        session.execute(delete(Company).where(Company.inn == inn))
        session.add(Company(inn=inn, ogrn="1099000000199", name="ООО ПУБИЧНАЯ КАРТОЧКА", source="stage16_test"))
        session.commit()
    monkeypatch.setattr(
        company_aggregator,
        "fetch_external_sources",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external provider called")),
    )
    writes: list[str] = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(verb)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        html_response = client.get(f"/company/{inn}")
        api_response = client.get(f"/api/company/{inn}")
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        with get_session() as session:
            session.execute(delete(Company).where(Company.inn == inn))
            session.commit()
    assert html_response.status_code == 200
    assert api_response.status_code == 200
    assert writes == []


def test_public_company_projection_excludes_internal_and_raw_fields(monkeypatch):
    payload = {
        "id": 123,
        "inn": "7700000000",
        "ogrn": "1027700000000",
        "name": "ООО ТЕСТ",
        "status": "ACTIVE",
        "sources_used": ["fns_egrul_egrip"],
        "raw_evidence": {"secret": "must not leak"},
        "provider_error": "debug dump",
        "session_id": "private-session",
        "risk_v3": {
            "risk_score": 0,
            "label": "Низкий наблюдаемый риск",
            "coverage": {"coverage_score": 0, "workflow_completion_percent": 0},
        },
    }
    monkeypatch.setattr(main, "load_company_read_model", lambda _inn: payload)
    response = client.get("/api/company/7700000000")
    assert response.status_code == 200
    body = response.json()
    assert body["inn"] == "7700000000"
    assert body["sources"] == ["fns_egrul_egrip"]
    assert body["risk"] == {
        "score": 0,
        "label": "Низкий наблюдаемый риск",
        "coverage_score": 0,
        "workflow_completion_percent": 0,
    }
    for forbidden in ("id", "internal_id", "raw_evidence", "provider_error", "session_id"):
        assert forbidden not in body


def test_mutation_and_internal_routes_fail_closed(monkeypatch):
    monkeypatch.delenv("KONTRAGENT_INTERNAL_TOKEN", raising=False)
    assert client.post("/company/7700000000/risk-assessment").status_code == 503
    assert client.get("/internal/api/data-readiness").status_code == 503

    monkeypatch.setenv("KONTRAGENT_INTERNAL_TOKEN", "local-explicit-test-token")
    assert client.post(
        "/company/7700000000/risk-assessment",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    monkeypatch.setattr(main, "recalculate_company_risk", lambda _inn: None)
    response = client.post(
        "/company/7700000000/risk-assessment",
        headers={"Authorization": "Bearer local-explicit-test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_blocking_route_handlers_are_sync_functions():
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        if path.startswith(("/company", "/api/company", "/api/search", "/search", "/internal")) and methods:
            assert not inspect.iscoroutinefunction(route.endpoint), path
