from __future__ import annotations

import json
import os
import re

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
sync_playwright = playwright.sync_playwright

from public_app.repository import PublicRepository
from scripts.local_real_preview_support import INN, OWNER_EMAIL, validate_database_topology
from tests.test_workspace_vertical_slice_playwright import LiveServer
from public_app.main import create_app as create_public_app
from workspace_app.main import create_app as create_workspace_app


pytestmark = pytest.mark.skipif(
    os.getenv("LOCAL_REAL_PREVIEW_E2E") != "1",
    reason="set LOCAL_REAL_PREVIEW_E2E=1 after preparing disposable preview databases",
)


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).casefold()
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def test_real_alan_public_login_search_save_monitoring_report(monkeypatch):
    source_url = os.environ["ALAN_PREVIEW_SOURCE_DATABASE_URL"]
    operational_url = os.environ["DATABASE_URL"]
    public_url = os.environ["PUBLIC_DATABASE_URL"]
    validate_database_topology(
        source_url=source_url,
        operational_url=operational_url,
        public_url=public_url,
    )
    password = os.environ["NEXTCOMPANY_PREVIEW_PASSWORD"]
    monkeypatch.setenv("NEXTCOMPANY_LOCAL_REAL_PREVIEW", "1")
    monkeypatch.setenv("NEXTCOMPANY_DEMO_MODE", "0")
    monkeypatch.setenv("PUBLIC_FORCE_NOINDEX", "1")
    monkeypatch.setenv("PUBLIC_TRUSTED_HOSTS", "127.0.0.1,localhost,testserver")
    monkeypatch.setenv("WORKSPACE_TRUSTED_HOSTS", "127.0.0.1,localhost,testserver")

    repository = PublicRepository(public_url)
    engine = sa.create_engine(operational_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    workspace = LiveServer(
        create_workspace_app(public_repository=repository, session_factory=sessions)
    )
    public = LiveServer(create_public_app(repository, workspace_origin=workspace.url))
    with workspace, public, sync_playwright() as manager:
        browser = manager.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        response = page.goto(f"{public.url}/companies/{INN}")
        assert response and response.status == 200
        assert response.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
        expect(page.locator('[data-preview-mode="local-real"]')).to_be_visible()
        expect(page.get_by_role("heading", name='ООО "АЛАН"')).to_be_visible()
        expect(page.locator(".identity-grid")).to_contain_text(INN)
        expect(page.locator('[data-section-key="finances"]')).to_be_visible()
        expect(page.locator('[data-section-key="risk"]')).to_contain_text(
            "Оценка содержит ограничения"
        )
        expect(page.locator('[data-section-key="summary"]')).to_contain_text(
            "Недостаточно данных"
        )
        expect(page.locator(".fact-source").first).to_contain_text("Источник:")
        public_revision = page.locator(".company-page").get_attribute("data-view-revision")
        assert public_revision and public_revision.startswith("cv1:")

        api = httpx.get(f"{public.url}/api/company/{INN}", timeout=5)
        assert api.status_code == 200
        forbidden = {
            "company_id",
            "workspace_id",
            "raw_payload",
            "raw_response",
            "password_hash",
        }
        assert not forbidden.intersection(_all_keys(api.json()))
        assert "/Users/" not in api.text and "/private/" not in api.text

        page.get_by_role("link", name="Открыть в кабинете").click()
        expect(page).to_have_url(re.compile(rf"{re.escape(workspace.url)}/login"))
        page.get_by_label("Email").fill(OWNER_EMAIL)
        page.get_by_label("Пароль").fill(password)
        page.get_by_role("button", name="Войти").click()
        expect(page).to_have_url(f"{workspace.url}/app/companies/{INN}")
        expect(page.locator('[data-preview-mode="local-real"]')).to_be_visible()

        page.goto(f"{workspace.url}/app/search?q={INN}")
        page.get_by_role("link", name=re.compile('ООО "АЛАН"')).click()
        authorized = page.locator(".company-head")
        expect(authorized).to_have_attribute("data-view-revision", public_revision)
        expect(authorized).to_contain_text(INN)

        save = page.get_by_role("button", name="Сохранить компанию")
        if save.count():
            save.click()
        expect(page.locator(".company-head")).to_have_attribute("data-saved", "true")

        page.get_by_role("link", name="Открыть статус мониторинга").click()
        expect(page.locator("[data-preview-monitoring-limit]")).to_contain_text(
            "Live-проверки отключены"
        )
        enable = page.get_by_role("button", name="Включить мониторинг")
        if enable.count():
            enable.click()
        expect(page.locator("[data-monitoring-state]")).to_have_attribute(
            "data-monitoring-state", "ACTIVE"
        )

        page.goto(f"{workspace.url}/app/companies/{INN}")
        missing_csrf = page.request.post(
            f"{workspace.url}/app/companies/{INN}/reports", form={}
        )
        assert missing_csrf.status == 403
        page.get_by_role("button", name="Сформировать отчёт").click()
        expect(page).to_have_url(re.compile(rf"{re.escape(workspace.url)}/app/reports/"))
        export_link = page.get_by_role("link", name=re.compile("JSON"))
        href = export_link.get_attribute("href")
        assert href
        report_response = page.request.get(f"{workspace.url}{href}")
        assert report_response.status == 200
        report = json.loads(report_response.body())
        assert report["company"]["inn"] == INN
        report_text = json.dumps(report, ensure_ascii=False)
        assert "risk" in report and "summary" in report
        assert "company_id" not in report_text and "workspace_id" not in report_text
        browser.close()

    with Session(engine) as session:
        assert session.scalar(sa.text("SELECT count(*) FROM monitoring_events")) == 0
        assert session.scalar(sa.text("SELECT count(*) FROM workspace_feed_entries")) == 0
        assert session.scalar(sa.text("SELECT count(*) FROM monitoring_subscriptions")) == 1
    engine.dispose()
