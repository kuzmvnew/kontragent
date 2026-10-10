from __future__ import annotations

from types import SimpleNamespace

from public_app.presentation import (
    company_status_label,
    company_status_tone,
    field_label,
    freshness_label,
    monitoring_status_label,
    public_value,
    ui_label,
)
from workspace_app.presentation import build_company_themes, workspace_value


def test_legal_status_map_is_explicit_and_unknown_values_fail_closed():
    assert company_status_label("ACTIVE") == "Действующая организация"
    assert company_status_label("LIQUIDATING") == "Организация находится в процессе ликвидации"
    assert company_status_label("SOME_NEW_STATE") == "Статус организации не определён"
    assert company_status_label("Новый неизвестный статус") == "Статус организации не определён"
    assert company_status_label(None) == "Статус организации не определён"
    assert company_status_tone("ACTIVE") == "active"
    assert company_status_tone("SOME_NEW_STATE") == "unknown"


def test_contract_enums_are_localized_at_the_presentation_boundary():
    assert ui_label("CURRENT") == "Актуальные сведения"
    assert freshness_label("STALE") == "Требуют обновления"
    assert monitoring_status_label("NOT_ACTIVE") == "Мониторинг не подключён"
    assert ui_label("BRAND_NEW_ENUM") == "Не определено"
    assert field_label("regime") == "Система налогообложения"
    assert field_label("future_contract_key") == "Сведения"
    assert public_value({"status": "CURRENT", "person_ref": "internal:42"}) == (
        "Статус: Актуальные сведения"
    )
    assert workspace_value({"status": "ACTIVE", "related_person_ref": "internal:43"}) == (
        "Статус: Включено"
    )


def test_company_view_sections_are_grouped_without_changing_the_contract():
    identity = SimpleNamespace(
        section_key="identity",
        title="Идентификация",
        state="FOUND",
        items=(
            SimpleNamespace(field_key="inn"),
            SimpleNamespace(field_key="full_name"),
            SimpleNamespace(field_key="okpo"),
        ),
    )
    courts = SimpleNamespace(
        section_key="courts",
        title="Суды",
        state="NOT_CHECKED",
        items=(),
    )
    view = SimpleNamespace(sections=(identity, courts))

    presented = build_company_themes(view)
    themes = {theme["id"]: theme for theme in presented["themes"]}

    assert tuple(themes) == (
        "requisites",
        "leadership",
        "finances",
        "tax",
        "courts",
        "enforcement",
        "licenses",
        "connections",
    )
    assert [item.field_key for item in themes["requisites"]["sections"][0]["items"]] == [
        "okpo"
    ]
    assert themes["courts"]["sections"][0]["state"] == "NOT_CHECKED"
    assert identity.items[0].field_key == "inn"
