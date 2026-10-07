from __future__ import annotations

import re
import socket
import threading
import time
from urllib.parse import quote
from uuid import uuid4

import httpx
import sqlalchemy as sa
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from app.models.monitoring import MonitoringSubscription
from app.models.semantic_fact import CompanySemanticFact
from app.models.workspace import SavedCompany, WorkspaceMembership
from tests.test_monitoring_p0 import NOW, _enable_entitlement, _semantic_fact, _set_fact
from public_app.main import create_app as create_public_app
from tests.public_test_support import projection
from tests.test_workspace_p0 import (
    PASSWORD,
    FakePublicRepository,
    _bootstrap,
    _cleanup,
    _grant_membership,
)
from workspace_app.main import create_app as create_workspace_app
from workspace_app.monitoring_service import monitor_company_once, subscribe_company
from workspace_app.service import save_company


class LiveServer:
    def __init__(self, app) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                access_log=False,
                lifespan="off",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            try:
                if httpx.get(f"{self.url}/login", timeout=0.5).status_code < 500:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        self.server.should_exit = True
        self.thread.join(timeout=5)
        raise RuntimeError(f"test server did not start at {self.url}")

    def __exit__(self, *_args) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _apps(item):
    repository = FakePublicRepository((item,))
    workspace = LiveServer(
        create_workspace_app(
            public_repository=repository,
            session_factory=SessionLocal,
        )
    )
    public = LiveServer(
        create_public_app(
            repository,
            workspace_origin=workspace.url,
        )
    )
    return public, workspace


def _seed_company(item) -> None:
    with Session(engine) as session:
        session.add(
            Company(
                inn=item.company.inn,
                name=item.company.name,
                entity_type="legal",
            )
        )
        session.commit()


def _login(page, email: str) -> None:
    page.get_by_label("Email").fill(email)
    page.get_by_label("Пароль").fill(PASSWORD)
    page.get_by_role("button", name="Войти").click()


def test_browser_public_login_search_save_saved_open_and_unsave():
    email = f"browser-vertical-{uuid4()}@example.test"
    item = projection(sequence=100_100_131)
    try:
        _seed_company(item)
        _bootstrap(email, "Browser Workspace", saved_limit=3)
        public, workspace = _apps(item)
        with workspace, public, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            page.goto(f"{public.url}/companies/{item.company.inn}")
            page.get_by_role("link", name="Открыть в кабинете").click()
            expect(page).to_have_url(re.compile(rf"{re.escape(workspace.url)}/login"))
            _login(page, email)
            expect(page).to_have_url(
                f"{workspace.url}/app/companies/{item.company.inn}"
            )
            expect(page.locator(".workspace-context")).to_contain_text(
                "Browser Workspace"
            )

            page.get_by_role("link", name="NEXT Company", exact=True).click()
            expect(page.get_by_role("heading", name="Browser Workspace")).to_be_visible()
            expect(page.get_by_text("Владелец").first).to_be_visible()
            page.get_by_role("link", name="Поиск", exact=True).click()
            page.get_by_label("Название или ИНН").fill(item.company.inn)
            page.get_by_role("button", name="Найти").click()
            page.get_by_role("link", name=re.compile(item.company.name)).click()
            expect(page.locator(".company-head")).to_have_attribute(
                "data-company-inn", item.company.inn
            )

            page.get_by_role("button", name="Сохранить компанию").click()
            expect(page.get_by_role("status")).to_contain_text("Компания сохранена")
            expect(page.locator(".company-head")).to_have_attribute(
                "data-saved", "true"
            )
            expect(page.locator(".monitoring-preview")).to_have_attribute(
                "data-monitoring-state", "NOT_ACTIVE"
            )
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )

            page.get_by_role("link", name="Сохранённые", exact=True).click()
            expect(page.get_by_text(item.company.name, exact=True)).to_be_visible()
            page.get_by_role("link", name="Открыть карточку").click()
            expect(page.get_by_text("Сохранено", exact=True)).to_be_visible()
            page.get_by_role("button", name="Удалить из сохранённых").click()
            page.get_by_role("link", name="Сохранённые", exact=True).click()
            expect(page.get_by_text("Пока нет сохранённых компаний")).to_be_visible()
            browser.close()
    finally:
        _cleanup(email, inns=(item.company.inn,))


def test_browser_monitoring_enable_event_feed_and_pause():
    email = f"browser-monitoring-{uuid4()}@example.test"
    item = projection(sequence=100_100_152)
    try:
        _seed_company(item)
        _user_id, workspace_id = _bootstrap(email, "Browser Monitoring")
        with Session(engine) as session:
            _enable_entitlement(session, workspace_id)
            company_id = session.scalar(
                sa.select(Company.id).where(Company.inn == item.company.inn)
            )
            _semantic_fact(
                session,
                company_id,
                section="ADDRESS",
                field="ADDRESS",
                value="Старый адрес",
            )
            session.commit()
        public, workspace = _apps(item)
        with workspace, public, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{workspace.url}/login")
            _login(page, email)
            page.goto(f"{workspace.url}/app/companies/{item.company.inn}")
            page.get_by_role("button", name="Сохранить компанию").click()
            page.get_by_role("link", name="Открыть статус мониторинга").click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "NOT_ACTIVE"
            )
            page.get_by_role("button", name="Включить мониторинг").click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "ACTIVE"
            )
            with Session(engine) as session:
                fact = session.scalar(
                    sa.select(CompanySemanticFact).where(
                        CompanySemanticFact.company_id == company_id,
                        CompanySemanticFact.field_key == "ADDRESS",
                    )
                )
                _set_fact(fact, value="Новый адрес")
                session.flush()
                result = monitor_company_once(session, company_id=company_id)
                assert result.canonical_event_count == 1
                assert result.feed_entry_count == 1
                session.commit()
            page.get_by_role("link", name="Лента мониторинга").click()
            expect(page.get_by_text("Новый адрес")).to_be_visible()
            expect(page.get_by_text("Старый адрес")).to_be_visible()
            page.get_by_role("link", name="Открыть карточку").click()
            expect(page.locator(".company-head")).to_have_attribute(
                "data-company-inn", item.company.inn
            )
            page.get_by_role("link", name="Открыть статус мониторинга").click()
            page.get_by_role("button", name="Приостановить мониторинг").click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "PAUSED"
            )
            browser.close()
    finally:
        _cleanup(email, inns=(item.company.inn,))


def test_browser_paused_unsaved_monitoring_requires_resave_before_resume():
    email = f"browser-monitoring-recovery-{uuid4()}@example.test"
    item = projection(sequence=100_100_154)
    try:
        _seed_company(item)
        _user_id, workspace_id = _bootstrap(email, "Browser Monitoring Recovery")
        with Session(engine) as session:
            _enable_entitlement(session, workspace_id)
            session.commit()

        _public, workspace = _apps(item)
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            page.goto(f"{workspace.url}/login")
            _login(page, email)
            page.get_by_role("link", name="Поиск", exact=True).click()
            page.get_by_label("Название или ИНН").fill(item.company.inn)
            page.get_by_role("button", name="Найти").click()
            page.get_by_role("link", name=re.compile(item.company.name)).click()
            page.get_by_role("button", name="Сохранить компанию").click()
            page.get_by_role("link", name="Открыть статус мониторинга").click()
            page.get_by_role("button", name="Включить мониторинг").click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "ACTIVE"
            )
            page.get_by_role("button", name="Приостановить мониторинг").click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "PAUSED"
            )
            page.get_by_role("link", name="Вернуться к карточке").click()
            page.get_by_role("button", name="Удалить из сохранённых").click()
            expect(page.locator(".company-head")).to_have_attribute(
                "data-saved", "false"
            )

            page.get_by_role("link", name="Мониторинг", exact=True).click()
            subscription = page.locator(".subscription-row")
            expect(subscription).to_have_count(1)
            expect(subscription).to_have_attribute("data-monitoring-state", "PAUSED")
            expect(subscription).to_have_attribute("data-saved", "false")
            expect(subscription).to_have_attribute("data-can-resume", "false")
            expect(
                subscription.get_by_role("button", name="Возобновить", exact=True)
            ).to_have_count(0)
            expect(
                subscription.get_by_text(
                    "Для возобновления мониторинга сначала снова сохраните компанию."
                )
            ).to_be_visible()

            subscription.get_by_role("link", name="Открыть компанию").click()
            expect(page.locator(".company-head")).to_have_attribute(
                "data-saved", "false"
            )
            page.get_by_role("button", name="Сохранить компанию").click()
            expect(page.locator(".company-head")).to_have_attribute(
                "data-saved", "true"
            )
            page.get_by_role("link", name="Мониторинг", exact=True).click()
            subscription = page.locator(".subscription-row")
            expect(subscription).to_have_attribute("data-monitoring-state", "PAUSED")
            expect(subscription).to_have_attribute("data-saved", "true")
            expect(subscription).to_have_attribute("data-can-resume", "true")
            subscription.get_by_role("button", name="Возобновить", exact=True).click()
            expect(page.locator(".subscription-row")).to_have_attribute(
                "data-monitoring-state", "ACTIVE"
            )
            expect(page.locator(".subscription-row")).to_have_attribute(
                "data-can-pause", "true"
            )
            assert page_errors == []
            browser.close()

        with Session(engine) as session:
            subscriptions = tuple(
                session.scalars(
                    sa.select(MonitoringSubscription).where(
                        MonitoringSubscription.workspace_id == workspace_id
                    )
                ).all()
            )
            assert len(subscriptions) == 1
            assert subscriptions[0].status == "ACTIVE"
    finally:
        _cleanup(email, inns=(item.company.inn,))


def test_browser_two_workspace_monitoring_feeds_are_separate():
    email_a = f"browser-monitoring-a-{uuid4()}@example.test"
    email_b = f"browser-monitoring-b-{uuid4()}@example.test"
    item = projection(sequence=100_100_153)
    try:
        _seed_company(item)
        user_a, workspace_a = _bootstrap(email_a, "Monitoring Tenant A")
        user_b, workspace_b = _bootstrap(email_b, "Monitoring Tenant B")
        with Session(engine) as session:
            company_id = session.scalar(
                sa.select(Company.id).where(Company.inn == item.company.inn)
            )
            _enable_entitlement(session, workspace_a)
            _enable_entitlement(session, workspace_b)
            save_company(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                inn=item.company.inn,
            )
            save_company(
                session,
                user_id=user_b,
                workspace_id=workspace_b,
                inn=item.company.inn,
            )
            fact = _semantic_fact(
                session,
                company_id,
                section="ADDRESS",
                field="ADDRESS",
                value="Before",
            )
            subscribe_company(
                session,
                user_id=user_a,
                workspace_id=workspace_a,
                inn=item.company.inn,
            )
            subscribe_company(
                session,
                user_id=user_b,
                workspace_id=workspace_b,
                inn=item.company.inn,
            )
            _set_fact(fact, value="After")
            session.flush()
            result = monitor_company_once(session, company_id=company_id)
            assert result.canonical_event_count == 1
            assert result.feed_entry_count == 2
            session.commit()

        _public, workspace = _apps(item)
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            pages = []
            for email, workspace_name in (
                (email_a, "Monitoring Tenant A"),
                (email_b, "Monitoring Tenant B"),
            ):
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"{workspace.url}/login")
                _login(page, email)
                page.goto(f"{workspace.url}/app/monitoring")
                expect(page.locator(".workspace-context")).to_contain_text(
                    workspace_name
                )
                expect(page.locator(".feed-item")).to_have_count(1)
                expect(page.get_by_text("After")).to_be_visible()
                pages.append((context, page))
            entry_a = pages[0][1].locator(".feed-item").get_attribute(
                "data-feed-entry-id"
            )
            entry_b = pages[1][1].locator(".feed-item").get_attribute(
                "data-feed-entry-id"
            )
            assert entry_a and entry_b and entry_a != entry_b
            csrf_a = next(
                cookie["value"]
                for cookie in pages[0][0].cookies()
                if cookie["name"] == "nextcompany_csrf"
            )
            denied = pages[0][0].request.post(
                f"{workspace.url}/app/api/monitoring/feed/{entry_b}/read",
                headers={"x-csrf-token": csrf_a},
            )
            assert denied.status == 404
            for context, _page in pages:
                context.close()
            browser.close()
    finally:
        _cleanup(email_a, email_b, inns=(item.company.inn,))


def test_browser_two_workspace_selection_keeps_destination_and_isolates_saved():
    email = f"browser-multi-{uuid4()}@example.test"
    other_email = f"browser-multi-other-{uuid4()}@example.test"
    item = projection(sequence=100_100_132)
    try:
        _seed_company(item)
        user_id, workspace_a = _bootstrap(email, "Browser Tenant A", saved_limit=3)
        _other_user_id, workspace_b = _bootstrap(
            other_email,
            "Browser Tenant B",
            saved_limit=3,
        )
        _grant_membership(user_id, workspace_b)
        public, workspace = _apps(item)
        with workspace, public, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            page.goto(f"{public.url}/companies/{item.company.inn}")
            page.get_by_role("link", name="Открыть в кабинете").click()
            _login(page, email)
            expect(page).to_have_url(
                re.compile(rf"{re.escape(workspace.url)}/workspace/select\?return_to=")
            )
            tenant_a = page.locator("form.card").filter(has_text="Browser Tenant A")
            tenant_a.get_by_role("button", name="Открыть").click()
            expect(page).to_have_url(
                f"{workspace.url}/app/companies/{item.company.inn}"
            )
            page.get_by_role("button", name="Сохранить компанию").click()
            page.get_by_role("link", name="Сохранённые", exact=True).click()
            expect(page.get_by_text(item.company.name, exact=True)).to_be_visible()

            page.get_by_role("link", name="Сменить Workspace").click()
            tenant_b = page.locator("form.card").filter(has_text="Browser Tenant B")
            tenant_b.get_by_role("button", name="Открыть").click()
            expect(page.locator(".workspace-context")).to_contain_text(
                "Browser Tenant B"
            )
            page.get_by_role("link", name="Сохранённые", exact=True).click()
            expect(page.get_by_text("Пока нет сохранённых компаний")).to_be_visible()
            expect(page.get_by_text(item.company.name, exact=True)).to_have_count(0)
            browser.close()

        with Session(engine) as session:
            saved_workspaces = tuple(
                session.scalars(
                    sa.select(SavedCompany.workspace_id)
                    .join(Company, Company.id == SavedCompany.company_id)
                    .where(Company.inn == item.company.inn)
                ).all()
            )
            memberships = tuple(
                session.scalars(
                    sa.select(WorkspaceMembership.workspace_id).where(
                        WorkspaceMembership.user_id == user_id,
                    )
                ).all()
            )
        assert set(memberships) == {workspace_a, workspace_b}
        assert saved_workspaces == (workspace_a,)
    finally:
        _cleanup(email, other_email, inns=(item.company.inn,))


def test_browser_malicious_return_to_always_lands_on_workspace_home():
    email = f"browser-return-negative-{uuid4()}@example.test"
    item = projection(sequence=100_100_133)
    destinations = (
        "/app/../admin",
        "/app/%2e%2e/admin",
        "/application",
        "//[bad",
        "/app/%00",
        "/app/%0a",
        "/app/%E2%80%AEfoo",
        "/app/%2500",
        "/app/search?q=%",
        "/app/search?q=%0",
        "/app/search?q=%GG",
        "/app/search?q=%25GG",
    )
    try:
        _bootstrap(email, "Browser Return Safety")
        _public, workspace = _apps(item)
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)

            for destination in destinations:
                context = browser.new_context()
                page = context.new_page()
                encoded = quote(destination, safe="")
                page.goto(f"{workspace.url}/login?return_to={encoded}")
                expect(page.locator('input[name="return_to"]')).to_have_value("/app")
                _login(page, email)
                expect(page).to_have_url(f"{workspace.url}/app")
                expect(
                    page.get_by_role("heading", name="Browser Return Safety")
                ).to_be_visible()
                assert "/admin" not in page.url
                context.close()

            browser.close()
    finally:
        _cleanup(email)


def test_browser_complete_workspace_product_flow():
    email = f"browser-product-completion-{uuid4()}@example.test"
    item = projection(sequence=100_100_182)
    try:
        _seed_company(item)
        _user_id, workspace_id = _bootstrap(
            email,
            "Complete Workspace",
            saved_limit=5,
        )
        with Session(engine) as session:
            _enable_entitlement(session, workspace_id)
            company_id = session.scalar(
                sa.select(Company.id).where(Company.inn == item.company.inn)
            )
            fact = _semantic_fact(
                session,
                company_id,
                section="ADDRESS",
                field="ADDRESS",
                value="Initial address",
            )
            fact_ref = fact.fact_ref
            session.commit()

        public, workspace = _apps(item)
        with workspace, public, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{workspace.url}/login")
            _login(page, email)
            expect(page).to_have_url(f"{workspace.url}/app")
            expect(page.get_by_role("heading", name="Complete Workspace")).to_be_visible()
            expect(page.get_by_text("Осталось мест")).to_be_visible()

            page.get_by_role("link", name="Поиск", exact=True).click()
            expect(page.locator('nav a[aria-current="page"]')).to_have_text("Поиск")
            page.get_by_label("Название или ИНН").fill(item.company.inn)
            page.get_by_role("button", name="Найти").click()
            page.get_by_role("link", name=re.compile(item.company.name)).click()
            page.get_by_role("button", name="Сохранить компанию").click()

            page.get_by_role("link", name="Сохранённые", exact=True).click()
            expect(page.locator('nav a[aria-current="page"]')).to_have_text("Сохранённые")
            page.get_by_text("Добавить заметку").click()
            page.get_by_label("Заметка").fill("  Проверить   договор  ")
            page.get_by_role("button", name="Сохранить заметку").click()
            expect(page.get_by_role("status")).to_contain_text("Заметка сохранена")
            expect(page.locator(".saved-note")).to_have_text("Проверить договор")

            page.get_by_role("link", name="Мониторинг", exact=True).last.click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "NOT_ACTIVE"
            )
            page.get_by_role("button", name="Включить мониторинг").click()
            expect(page.locator(".state-panel")).to_have_attribute(
                "data-monitoring-state", "ACTIVE"
            )

            with Session(engine) as session:
                fact = session.get(CompanySemanticFact, fact_ref)
                _set_fact(fact, value="Changed address")
                session.flush()
                result = monitor_company_once(session, company_id=company_id)
                assert result.feed_entry_count == 1
                session.commit()

            page.get_by_role("link", name="Лента мониторинга").click()
            expect(page.locator('nav a[aria-current="page"]')).to_have_text("Мониторинг")
            expect(page.locator(".subscription-row")).to_have_count(1)
            expect(page.get_by_text("Changed address")).to_be_visible()
            page.get_by_role("button", name="Отметить прочитанным").click()
            expect(page.get_by_text("Прочитано", exact=True)).to_be_visible()
            page.get_by_role("link", name="Открыть карточку").click()
            expect(page.locator(".company-head")).to_have_attribute(
                "data-company-inn", item.company.inn
            )
            expect(page.get_by_text("Проверить договор", exact=True)).to_be_visible()
            page.get_by_role("button", name="Приостановить").click()
            expect(page.locator(".monitoring-preview")).to_have_attribute(
                "data-monitoring-state", "PAUSED"
            )
            page.get_by_role("button", name="Возобновить").click()
            expect(page.locator(".monitoring-preview")).to_have_attribute(
                "data-monitoring-state", "ACTIVE"
            )
            page.get_by_role("button", name="Выйти").click()
            expect(page).to_have_url(f"{workspace.url}/login")
            browser.close()
    finally:
        _cleanup(email, inns=(item.company.inn,))


def test_browser_fresh_workspace_empty_dashboard_saved_and_monitoring():
    email = f"browser-empty-workspace-{uuid4()}@example.test"
    try:
        _bootstrap(email, "Fresh Workspace", saved_limit=3)
        item = projection(sequence=100_100_183)
        _public, workspace = _apps(item)
        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"{workspace.url}/login")
            _login(page, email)
            expect(page.get_by_text("Нет сохранённых компаний")).to_be_visible()
            expect(page.get_by_text("Мониторинг не подключён", exact=True)).to_be_visible()
            page.get_by_role("link", name="Сохранённые", exact=True).click()
            expect(page.get_by_text("Пока нет сохранённых компаний")).to_be_visible()
            page.get_by_role("link", name="Мониторинг", exact=True).click()
            expect(page.get_by_role("heading", name="Мониторинг", exact=True)).to_be_visible()
            expect(page.get_by_text("Мониторинг не подключён", exact=True)).to_be_visible()
            assert page_errors == []
            browser.close()
    finally:
        _cleanup(email)
