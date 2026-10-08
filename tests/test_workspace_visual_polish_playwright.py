from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from public_app.contracts import PublicCompanyViewV1, PublicViewSection
from public_app.main import create_app as create_public_app
from tests.public_test_support import projection
from tests.test_public_card_data_binding import NOW, _fact
from tests.test_workspace_p0 import FakePublicRepository, _bootstrap, _cleanup
from tests.test_workspace_vertical_slice_playwright import LiveServer, _login
from workspace_app.main import create_app as create_workspace_app
from workspace_app.service import save_company


DESKTOP = {"width": 1440, "height": 1000}
MOBILE = {"width": 390, "height": 844}


def _has_no_page_overflow(page: Page) -> bool:
    return page.evaluate(
        """
        () => document.documentElement.scrollWidth <= window.innerWidth
          && document.body.scrollWidth <= window.innerWidth
        """
    )


def _heading_size(page: Page) -> float:
    return page.locator("h1").first.evaluate(
        "element => parseFloat(getComputedStyle(element).fontSize)"
    )


def _capture(page: Page, directory: Path | None, name: str) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f"{name}.png"), full_page=True)


def _assert_keyboard_focus(page: Page) -> None:
    page.locator("body").press("Tab")
    focused = page.locator(":focus")
    expect(focused).to_have_count(1)
    outline = focused.evaluate(
        """
        element => {
          const style = getComputedStyle(element);
          return {style: style.outlineStyle, width: style.outlineWidth};
        }
        """
    )
    assert outline["style"] != "none"
    assert outline["width"] != "0px"


def test_workspace_visual_shell_dashboard_company_settings_and_screenshots(monkeypatch):
    email = f"visual-04a-{uuid4()}@example.test"
    base_item = projection(sequence=100_100_181)
    company_view = PublicCompanyViewV1(
        revision="cv1:" + "a" * 64,
        generated_at=NOW,
        inn=base_item.company.inn,
        sections=(
            PublicViewSection(
                section_key="registration",
                title="Регистрационные сведения",
                state="Сведения найдены",
                items=(
                    _fact(
                        "registration",
                        "legal_status",
                        "Действует",
                        source_name="ЕГРЮЛ ФНС России",
                    ),
                ),
            ),
            PublicViewSection(
                section_key="restrictions",
                title="Ограничения и предупреждения",
                state="Сведения пока не проверены",
            ),
        ),
        links={"public_card": f"/companies/{base_item.company.inn}"},
    )
    item = base_item.model_copy(update={"company_view": company_view})
    artifact_value = os.getenv("WORKSPACE_VISUAL_ARTIFACT_DIR", "").strip()
    artifact_dir = Path(artifact_value) if artifact_value else None

    try:
        with Session(engine) as session:
            session.add(
                Company(
                    inn=item.company.inn,
                    name=item.company.name,
                    entity_type="legal",
                )
            )
            session.commit()
        user_id, workspace_id = _bootstrap(
            email,
            "Visual QA Workspace",
            saved_limit=12,
        )
        with Session(engine) as session:
            save_company(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                inn=item.company.inn,
            )
            session.commit()

        monkeypatch.setenv("NEXTCOMPANY_DEMO_MODE", "1")
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

        with workspace, public, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport=DESKTOP)
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            page.goto(f"{workspace.url}/login")
            _login(page, email)

            # Dashboard — desktop shell, operational KPI set and keyboard focus.
            expect(page.locator(".sidebar")).to_be_visible()
            expect(page.locator(".topbar")).to_be_visible()
            expect(page.locator('.sidebar .nav-item[aria-current="page"]')).to_contain_text(
                "Главная"
            )
            expect(page.locator(".topbar-search input")).to_be_visible()
            expect(page.locator(".dashboard-search input")).to_be_visible()
            expect(page.locator(".kpi")).to_have_count(4)
            assert page.locator(".sidebar").evaluate(
                "element => Math.round(element.getBoundingClientRect().width)"
            ) == 224
            assert _heading_size(page) <= 38
            assert _has_no_page_overflow(page)
            _assert_keyboard_focus(page)
            _capture(page, artifact_dir, "01-dashboard-desktop")

            page.set_viewport_size(MOBILE)
            expect(page.locator(".sidebar")).to_be_visible()
            expect(page.locator(".sidebar-nav")).to_be_visible()
            assert page.locator(".sidebar").evaluate(
                "element => getComputedStyle(element).position"
            ) == "relative"
            expect(page.locator(".topbar-search input")).to_be_visible()
            assert _heading_size(page) <= 28
            assert _has_no_page_overflow(page)
            touch_heights = page.locator(
                ".sidebar-nav .nav-item, .topbar-search input, .topbar-search button"
            ).evaluate_all(
                "elements => elements.filter(el => el.offsetParent !== null).map(el => el.getBoundingClientRect().height)"
            )
            assert touch_heights and min(touch_heights) >= 44
            _capture(page, artifact_dir, "02-dashboard-mobile")

            # Authorized company card — real actions and factual dossier rows.
            page.set_viewport_size(DESKTOP)
            page.goto(f"{workspace.url}/app/companies/{item.company.inn}")
            expect(page.locator(".company-head")).to_have_attribute(
                "data-company-inn", item.company.inn
            )
            expect(page.locator(".local-nav")).to_be_visible()
            expect(page.locator(".analysis-block")).to_be_visible()
            expect(page.locator(".fact-section").first).to_be_visible()
            expect(page.locator(".source-row").first).to_be_visible()
            expect(page.get_by_role("button", name="Сформировать отчёт")).to_be_visible()
            expect(page.get_by_role("link", name="Публичная карточка")).to_be_visible()
            assert _heading_size(page) <= 38
            assert _has_no_page_overflow(page)
            _capture(page, artifact_dir, "03-company-desktop")

            page.set_viewport_size(MOBILE)
            expect(page.get_by_text("ИНН", exact=True).first).to_be_visible()
            expect(page.locator(".analysis-block")).to_be_visible()
            expect(page.locator(".action-bar")).to_be_visible()
            expect(page.locator(".monitoring-preview")).to_have_attribute(
                "data-monitoring-state", "NOT_ACTIVE"
            )
            assert _heading_size(page) <= 28
            assert _has_no_page_overflow(page)
            _capture(page, artifact_dir, "04-company-mobile")

            # Settings — readable labels, explicit numeric usage and usable form.
            page.set_viewport_size(DESKTOP)
            page.goto(f"{workspace.url}/app/settings")
            expect(page.locator('.sidebar .nav-item[aria-current="page"]')).to_contain_text(
                "Настройки"
            )
            expect(page.locator(".usage-row")).to_have_count(6)
            expect(page.get_by_text("Массовая проверка", exact=True).last).to_be_visible()
            expect(page.get_by_label("Название Workspace")).to_be_visible()
            assert _heading_size(page) <= 38
            assert _has_no_page_overflow(page)
            _capture(page, artifact_dir, "05-settings-desktop")

            page.set_viewport_size(MOBILE)
            expect(page.get_by_label("Название Workspace")).to_be_visible()
            expect(page.get_by_role("button", name="Сохранить название")).to_be_visible()
            assert _heading_size(page) <= 28
            assert _has_no_page_overflow(page)
            _capture(page, artifact_dir, "06-settings-mobile")

            assert page_errors == []
            browser.close()
    finally:
        _cleanup(email, inns=(item.company.inn,))
