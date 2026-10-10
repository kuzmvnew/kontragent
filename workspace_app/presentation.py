"""Presentation-only grouping and value formatting for Workspace pages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from public_app.presentation import (
    company_status_label,
    company_status_tone,
    field_label,
    freshness_label,
    monitoring_status_label,
    record_status_label,
    relation_status_label,
    ui_label,
)


_VALUE_LABELS = {
    "activity": "Вид деятельности",
    "amount": "Сумма",
    "amount_due": "Сумма взыскания",
    "amount_remaining": "Остаток",
    "contact_scope": "Принадлежность контакта",
    "contact_type": "Тип контакта",
    "count": "Количество",
    "currency": "Валюта",
    "date": "Дата",
    "department": "Подразделение",
    "description": "Описание",
    "document_type": "Тип документа",
    "identifier_type": "Тип идентификатора",
    "identifiers": "Идентификаторы",
    "individual_entrepreneur": "Статус индивидуального предпринимателя",
    "name": "Наименование",
    "number": "Номер",
    "person_name": "Связанное лицо",
    "position": "Должность",
    "record_count": "Количество записей",
    "region": "Регион",
    "related_company": "Связанная организация",
    "relation": "Тип связи",
    "relation_types": "Типы связи",
    "relations": "Связи",
    "result": "Результат",
    "role": "Роль",
    "role_context": "Контекст связи",
    "scope": "Область проверки",
    "share": "Доля",
    "since": "Сведения с",
    "started_on": "Дата начала",
    "state": "Состояние",
    "status": "Статус",
    "subject": "Предмет",
    "termination_date": "Дата прекращения",
    "total": "Итого",
    "total_due": "Сумма взысканий",
    "total_remaining": "Остаток",
    "type": "Тип",
    "until": "Сведения до",
    "value": "Значение",
    "current_status": "Период связи",
}

_HIDDEN_VALUE_KEYS = {
    "person_ref",
    "related_person_ref",
}

_CORE_REQUISITE_FIELDS = {
    "identity": {
        "entity_type",
        "full_name",
        "inn",
        "kpp",
        "name",
        "ogrn",
        "short_name",
    },
    "status": {"status", "legal_status"},
    "registration": {"registration_date"},
    "address": {"registered_address"},
}

_THEME_SPECS = (
    {
        "id": "requisites",
        "title": "Реквизиты",
        "section_keys": (
            "identity",
            "status",
            "registration",
            "address",
            "activity",
            "capital",
        ),
    },
    {
        "id": "leadership",
        "title": "Руководство и владельцы",
        "section_keys": ("management", "founders", "contacts"),
    },
    {
        "id": "finances",
        "title": "Финансы",
        "section_keys": ("finances", "employees"),
    },
    {"id": "tax", "title": "Налоги", "section_keys": ("tax",)},
    {
        "id": "courts",
        "title": "Суды",
        "section_keys": ("courts", "bankruptcy"),
    },
    {
        "id": "enforcement",
        "title": "Исполнительные производства",
        "section_keys": ("enforcement",),
    },
    {
        "id": "licenses",
        "title": "Лицензии",
        "section_keys": ("licenses",),
    },
    {
        "id": "connections",
        "title": "Связи",
        "section_keys": ("connections",),
    },
)

_OVERVIEW_KEYS = ("events", "procurement", "restrictions", "inspections")


def workspace_value(value, *, field_key: str | None = None) -> str:
    """Render minimized semantic values with Russian, non-technical labels."""

    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        parts = []
        for key, child in value.items():
            normalized_key = str(key)
            if normalized_key in _HIDDEN_VALUE_KEYS:
                continue
            label = _VALUE_LABELS.get(
                normalized_key,
                normalized_key.replace("_", " ").capitalize(),
            )
            parts.append(
                f"{label}: {workspace_value(child, field_key=normalized_key)}"
            )
        return " · ".join(parts) or "—"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "; ".join(
            workspace_value(item, field_key=field_key) for item in value
        ) or "—"
    if field_key == "current_status":
        return relation_status_label(value)
    if field_key in {"status", "state", "relation", "relation_types", "contact_type", "contact_scope", "identifier_type", "currency"}:
        return ui_label(value)
    return ui_label(value, fallback="Не определено")


def _presented_section(section, *, suppress_core: bool = False) -> dict:
    suppressed = _CORE_REQUISITE_FIELDS.get(section.section_key, set()) if suppress_core else set()
    return {
        "section_key": section.section_key,
        "title": section.title or "Сведения",
        "state": section.state,
        "items": tuple(item for item in section.items if item.field_key not in suppressed),
    }


def _sections(view, keys: tuple[str, ...], *, suppress_core: bool = False) -> tuple[dict, ...]:
    if view is None:
        return ()
    indexed = {section.section_key: section for section in view.sections}
    return tuple(
        _presented_section(indexed[key], suppress_core=suppress_core)
        for key in keys
        if key in indexed
    )


def build_company_themes(view) -> dict:
    """Group the unchanged Company View contract into customer themes."""

    return {
        "overview_sections": _sections(view, _OVERVIEW_KEYS),
        "themes": tuple(
            {
                "id": spec["id"],
                "title": spec["title"],
                "sections": _sections(
                    view,
                    spec["section_keys"],
                    suppress_core=spec["id"] == "requisites",
                ),
            }
            for spec in _THEME_SPECS
        ),
    }


__all__ = [
    "build_company_themes",
    "company_status_label",
    "company_status_tone",
    "field_label",
    "freshness_label",
    "monitoring_status_label",
    "record_status_label",
    "relation_status_label",
    "ui_label",
    "workspace_value",
]
