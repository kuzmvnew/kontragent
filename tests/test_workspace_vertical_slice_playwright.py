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
from app.models.workspace import SavedCompany, WorkspaceMembership
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
                assert "/admin" not in page.url
                context.close()

            browser.close()
    finally:
        _cleanup(email)
