from __future__ import annotations

import os
import re
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
from tests.test_workspace_p0 import (
    FakePublicRepository,
    _bootstrap,
    _cleanup,
    _grant_membership,
)
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


def _assert_visible_focus(page: Page, selector: str) -> None:
    control = page.locator(selector)
    control.focus()
    expect(control).to_be_focused()
    outline = control.evaluate(
        """
        element => {
          const style = getComputedStyle(element);
          return {style: style.outlineStyle, width: style.outlineWidth};
        }
        """
    )
    assert outline["style"] != "none"
    assert outline["width"] != "0px"


def _assert_sidebar_icon_system(page: Page) -> None:
    icons = page.locator(".sidebar-nav .nav-icon svg")
    expect(icons).to_have_count(8)
    geometry = icons.evaluate_all(
        """
        elements => elements.map(element => {
          const box = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return {
            viewBox: element.getAttribute('viewBox'),
            width: Math.round(box.width),
            height: Math.round(box.height),
            strokeWidth: style.strokeWidth,
          };
        })
        """
    )
    assert {item["viewBox"] for item in geometry} == {"0 0 24 24"}
    assert len({item["width"] for item in geometry}) == 1
    assert len({item["height"] for item in geometry}) == 1
    assert geometry[0]["width"] >= 20
    assert geometry[0]["height"] >= 20
    assert {item["strokeWidth"] for item in geometry} == {"1.8px"}


def _assert_search_text_clearance(page: Page) -> None:
    search = page.locator("#global-q")
    search.fill("ООО АЛАН длинный поисковый запрос для проверки поля")
    search.focus()
    expect(search).to_be_focused()
    geometry = page.evaluate(
        """
        () => {
          const input = document.querySelector('#global-q');
          const icon = document.querySelector('.topbar-search .search-icon');
          const inputBox = input.getBoundingClientRect();
          const iconBox = icon.getBoundingClientRect();
          const style = getComputedStyle(input);
          return {
            textStart: inputBox.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
            iconRight: iconBox.right,
          };
        }
        """
    )
    assert geometry["textStart"] - geometry["iconRight"] >= 8
    _assert_visible_focus(page, "#global-q")


def _assert_anchor_navigation(page: Page, label: str, anchor: str) -> None:
    page.locator(".company-local-nav").get_by_role(
        "link", name=label, exact=True
    ).click()
    expect(page).to_have_url(re.compile(rf"#{re.escape(anchor)}$"))
    target = page.locator(f"#{anchor}")
    expect(target).to_be_visible()
    page.wait_for_function(
        "anchor => { const box = document.querySelector('#' + anchor).getBoundingClientRect(); return box.top >= 0 && box.top < innerHeight; }",
        arg=anchor,
    )
    box = target.bounding_box()
    assert box is not None
    assert 0 <= box["y"] < page.viewport_size["height"]
    page.evaluate(
        "history.replaceState(null, '', location.pathname + location.search); window.scrollTo(0, 0)"
    )


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
            _assert_sidebar_icon_system(page)
            _assert_search_text_clearance(page)
            assert page.locator(".wordmark").first.evaluate(
                "element => parseFloat(getComputedStyle(element).fontSize)"
            ) >= 22
            assert page.locator(".sidebar").evaluate(
                "element => Math.round(element.getBoundingClientRect().width)"
            ) == 224
            desktop_workspace_switch = page.get_by_role(
                "link", name="Сменить Workspace", exact=True
            )
            expect(desktop_workspace_switch).to_be_visible()
            expect(desktop_workspace_switch).to_have_attribute(
                "href", "/workspace/select"
            )
            expect(page.get_by_role("button", name="Выйти")).to_be_visible()
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
            _assert_sidebar_icon_system(page)
            _assert_search_text_clearance(page)
            assert page.locator(".wordmark").first.evaluate(
                "element => parseFloat(getComputedStyle(element).fontSize)"
            ) >= 22
            mobile_workspace_switch = page.get_by_role(
                "link", name="Сменить Workspace", exact=True
            )
            expect(mobile_workspace_switch).to_be_visible()
            expect(mobile_workspace_switch).to_have_attribute(
                "href", "/workspace/select"
            )
            expect(page.get_by_role("button", name="Выйти")).to_be_visible()
            assert mobile_workspace_switch.evaluate(
                "element => element.tabIndex >= 0"
            )
            switch_box = mobile_workspace_switch.bounding_box()
            assert switch_box is not None
            assert switch_box["height"] >= 44 or switch_box["width"] >= 44
            _assert_visible_focus(page, ".workspace-switch")
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
            expect(page.get_by_text("Действующая организация", exact=True)).to_be_visible()
            for label in (
                "Обзор",
                "Реквизиты",
                "Руководство и владельцы",
                "Финансы",
                "Налоги",
                "Суды",
                "Исполнительные производства",
                "Лицензии",
                "Связи",
                "Источники и актуальность",
            ):
                expect(page.locator(".company-local-nav").get_by_role("link", name=label, exact=True)).to_be_visible()
            _assert_anchor_navigation(page, "Налоги", "tax")
            visible_text = page.locator("body").inner_text()
            for technical in ("COMPANY VIEW", "company-view-v1", "IDENTITY", "ACTIVE", "CURRENT"):
                assert technical not in visible_text
            action_heights = page.locator(
                ".company-actions button, .company-actions .button, .company-actions .action-control"
            ).evaluate_all(
                "elements => elements.filter(el => el.offsetParent !== null).map(el => Math.round(el.getBoundingClientRect().height))"
            )
            assert action_heights and max(action_heights) - min(action_heights) <= 1
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
            _assert_anchor_navigation(page, "Источники и актуальность", "sources")
            assert _heading_size(page) <= 28
            assert _has_no_page_overflow(page)
            _capture(page, artifact_dir, "04-company-mobile")

            # Public card — the same customer vocabulary and thematic structure.
            page.set_viewport_size(DESKTOP)
            page.goto(f"{public.url}/companies/{item.company.inn}")
            expect(page.get_by_text("Действующая организация", exact=True)).to_be_visible()
            expect(page.locator(".company-local-nav")).to_be_visible()
            public_text = page.locator("body").inner_text()
            for technical in ("COMPANY VIEW", "company-view-v1", "IDENTITY", "ACTIVE", "CURRENT"):
                assert technical not in public_text
            _assert_anchor_navigation(page, "Налоги", "tax")
            assert _has_no_page_overflow(page)

            page.set_viewport_size(MOBILE)
            expect(page.locator(".company-local-nav")).to_be_visible()
            _assert_anchor_navigation(page, "Источники и актуальность", "sources")
            assert _has_no_page_overflow(page)

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


def test_workspace_select_keeps_logout_visible_without_workspace_context():
    email = f"visual-logout-{uuid4()}@example.test"
    other_email = f"visual-logout-other-{uuid4()}@example.test"
    try:
        user_id, _workspace_a = _bootstrap(email, "Logout Visual A")
        _other_user_id, workspace_b = _bootstrap(other_email, "Logout Visual B")
        _grant_membership(user_id, workspace_b)
        workspace = LiveServer(
            create_workspace_app(
                public_repository=FakePublicRepository(()),
                session_factory=SessionLocal,
            )
        )

        with workspace, sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            page = browser.new_page(viewport=DESKTOP)
            page.goto(f"{workspace.url}/login")
            expect(page.locator("form[action='/logout']")).to_have_count(0)
            _login(page, email)
            expect(page).to_have_url(f"{workspace.url}/workspace/select")

            logout = page.get_by_role("button", name="Выйти")
            expect(logout).to_be_visible()
            expect(page.locator(".workspace-switch")).to_have_count(0)
            assert _has_no_page_overflow(page)

            page.set_viewport_size(MOBILE)
            expect(logout).to_be_visible()
            expect(page.locator(".workspace-switch")).to_have_count(0)
            logout_box = logout.bounding_box()
            assert logout_box is not None
            assert logout_box["height"] >= 44 or logout_box["width"] >= 44
            _assert_visible_focus(page, "form[action='/logout'] button")
            assert _has_no_page_overflow(page)

            logout.click()
            expect(page).to_have_url(f"{workspace.url}/login")
            expect(page.locator("form[action='/logout']")).to_have_count(0)
            browser.close()
    finally:
        _cleanup(email, other_email)
