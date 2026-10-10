from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "workspace_app" / "static" / "workspace.css"
TEMPLATE_ROOT = ROOT / "workspace_app" / "templates"


def test_workspace_css_uses_canonical_tokens_and_removes_ai_saas_treatments():
    css = CSS_PATH.read_text(encoding="utf-8")
    for token in (
        "#111512",
        "#343b36",
        "#3f7657",
        "#35664b",
        "#2d5741",
        "#6e917c",
        "#f1f7f3",
        "#ffffff",
        "#f7f9f8",
        "#dce2dd",
        "#c7d0c9",
        "#68736c",
        "#2e7a55",
        "#b87813",
        "#b53b36",
    ):
        assert token in css.casefold()
    lowered = css.casefold()
    assert "gradient(" not in lowered
    assert "box-shadow" not in lowered
    assert "georgia" not in lowered
    assert "times new roman" not in lowered
    assert '"pt sans caption"' in lowered
    assert "prefers-reduced-motion: reduce" in lowered


def test_workspace_css_exposes_reusable_operational_primitives():
    css = CSS_PATH.read_text(encoding="utf-8")
    for class_name in (
        "app-shell",
        "sidebar",
        "topbar",
        "nav-group",
        "nav-item",
        "page-header",
        "search-control",
        "kpi",
        "panel",
        "data-list",
        "data-row",
        "status-badge",
        "action-bar",
        "empty-state",
        "fact-section",
        "fact-row",
        "source-row",
        "analysis-block",
        "usage-row",
    ):
        assert re.search(rf"\.{re.escape(class_name)}(?:\W|$)", css)


def test_workspace_templates_keep_live_controls_and_frozen_action_contracts():
    templates = {
        path.name: path.read_text(encoding="utf-8")
        for path in TEMPLATE_ROOT.glob("*.html")
    }
    combined = "\n".join(templates.values())
    assert 'href="#"' not in combined
    assert "javascript:void(0)" not in combined
    assert 'action="/app/search"' in templates["base.html"]
    assert 'placeholder="Название или ИНН"' in templates["base.html"]
    assert 'aria-current="page"' in templates["base.html"]
    assert 'action="/app/companies/{{ company.inn }}/save"' in templates["company.html"]
    assert 'action="/app/companies/{{ company.inn }}/unsave"' in templates["company.html"]
    assert 'action="/app/companies/{{ company.inn }}/reports"' in templates["company.html"]
    assert 'data-company-inn="{{ company.inn }}"' in templates["company.html"]
    assert 'data-saved="{{' in templates["company.html"]
    assert 'data-monitoring-state="{{ actions.monitoring.state }}"' in templates[
        "company.html"
    ]


def test_workspace_shell_has_only_real_routes_and_requested_grouping():
    base = (TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8")
    for label in (
        "Главная",
        "Поиск",
        "Сохранённые",
        "Мониторинг",
        "Массовая проверка",
        "Отчёты",
        "Пользователи",
        "Настройки",
    ):
        assert label in base
    for group in ("Работа", "Команда", "Аккаунт"):
        assert group in base
    assert "Роли и доступы" not in base
    assert "Тариф и лимиты" not in base
    assert "next. company" not in base  # wordmark stays semantic markup, not fallback text.
    assert 'aria-label="NEXT Company"' in base


def test_workspace_shell_uses_one_svg_icon_system_and_protects_search_text():
    base = (TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8")
    css = CSS_PATH.read_text(encoding="utf-8")

    assert base.count("{{ nav_item(") == 8
    for icon in ("home", "search", "saved", "monitoring", "bulk", "reports", "users", "settings"):
        assert f"icon == '{icon}'" in base
    assert '<svg viewBox="0 0 24 24"' in base
    assert "stroke-width: 1.8" in css
    assert ".topbar-search #global-q" in css
    assert "padding: 10px 12px 10px 44px" in css


def test_company_card_exposes_the_required_russian_navigation():
    company = (TEMPLATE_ROOT / "company.html").read_text(encoding="utf-8")
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
        assert f">{label}</a>" in company
    assert "Это не означает отсутствия фактов" in company
