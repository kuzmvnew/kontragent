"""Customer-facing labels for public and authenticated company screens.

The projection contracts deliberately retain stable enum values.  This module
is the presentation boundary that keeps those values out of ordinary UI while
leaving API payloads and diagnostic attributes unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import re


_LEGAL_STATUS_LABELS = {
    "ACTIVE": "Действующая организация",
    "ДЕЙСТВУЕТ": "Действующая организация",
    "ДЕЙСТВУЮЩАЯ": "Действующая организация",
    "INACTIVE": "Деятельность организации прекращена",
    "TERMINATED": "Деятельность организации прекращена",
    "ПРЕКРАТИЛА ДЕЯТЕЛЬНОСТЬ": "Деятельность организации прекращена",
    "LIQUIDATING": "Организация находится в процессе ликвидации",
    "В ПРОЦЕССЕ ЛИКВИДАЦИИ": "Организация находится в процессе ликвидации",
    "LIQUIDATED": "Организация ликвидирована",
    "ЛИКВИДИРОВАНА": "Организация ликвидирована",
    "REORGANIZATION": "Организация находится в процессе реорганизации",
    "REORGANIZING": "Организация находится в процессе реорганизации",
    "В ПРОЦЕССЕ РЕОРГАНИЗАЦИИ": "Организация находится в процессе реорганизации",
    "EXCLUDED": "Организация исключена из ЕГРЮЛ",
    "ИСКЛЮЧЕНА ИЗ ЕГРЮЛ": "Организация исключена из ЕГРЮЛ",
    "BANKRUPTCY": "Организация находится в процедуре банкротства",
    "BANKRUPT": "Организация находится в процедуре банкротства",
}

_UNKNOWN_LEGAL_STATUSES = {
    "",
    "UNKNOWN",
    "UNAVAILABLE",
    "NOT_AVAILABLE",
    "NOT KNOWN",
    "N/A",
    "NONE",
    "NULL",
}

_UI_LABELS = {
    "ACTIVE": "Включено",
    "PAUSED": "Приостановлено",
    "NOT_ACTIVE": "Не подключено",
    "CURRENT": "Актуальные сведения",
    "HISTORICAL": "Исторические сведения",
    "STALE": "Требуют обновления",
    "UNKNOWN": "Не определено",
    "FOUND": "Сведения найдены",
    "NOT_FOUND": "Сведения не найдены в завершённой проверке",
    "NOT_APPLICABLE": "Проверка неприменима",
    "NOT_CHECKED": "Сведения пока не проверены",
    "SOURCE_UNAVAILABLE": "Источник временно недоступен",
    "TIMEOUT": "Источник не ответил вовремя",
    "PARSING_ERROR": "Сведения источника не удалось обработать",
    "STALE_DATA": "Сведения требуют обновления",
    "PARTIAL": "Доступна часть сведений",
    "CONFLICTING_EVIDENCE": "Источники содержат различающиеся сведения",
    "READY": "Готово",
    "RUNNING": "Выполняется",
    "PENDING": "Ожидает",
    "EXPIRED": "Срок истёк",
    "FAILED": "Ошибка",
    "CANCELLED": "Отменено",
    "COMPLETED": "Завершено",
    "COMPLETED_WITH_ERRORS": "Завершено с ошибками",
    "INVALID_INN": "Некорректный ИНН",
    "DUPLICATE": "Дубликат",
    "NOT_RESOLVED": "Компания не определена",
    "NOT_READY": "Данные не готовы",
    "PROCESSING_ERROR": "Ошибка обработки",
    "QUEUED": "В очереди",
    "SUSPENDED": "Приостановлено",
    "REVOKED": "Отозвано",
    "HIGH": "Высокая",
    "MEDIUM": "Средняя",
    "LOW": "Низкая",
    "INFO": "Информационная",
    "OWNER": "Владелец",
    "ADMIN": "Администратор",
    "MEMBER": "Участник",
    "VIEWER": "Наблюдатель",
    "LEGAL": "Юридическое лицо",
    "MANAGER": "Руководитель",
    "FOUNDER": "Учредитель",
    "PARTICIPANT": "Участник",
    "INDIVIDUAL_ENTREPRENEUR": "Индивидуальный предприниматель",
    "OTHER_PUBLIC_RELATION": "Иная публичная связь",
    "CORPORATE": "Корпоративный",
    "PERSONAL": "Личный",
    "PHONE": "Телефон",
    "EMAIL": "Электронная почта",
    "INN": "ИНН",
    "OGRNIP": "ОГРНИП",
    "RUB": "Российский рубль",
    "YEAR": "Год",
    "QUARTER": "Квартал",
    "DATE": "Дата",
    "REVENUE": "Выручка",
    "EXPENSES": "Расходы",
    "PROFIT_LOSS": "Прибыль или убыток",
    "NET_PROFIT": "Чистая прибыль",
    "EQUITY": "Капитал",
    "COMPANY_VALUE": "Стоимость компании",
    "EMPLOYEE_COUNT": "Численность сотрудников",
    "DEBT": "Налоговая задолженность",
    "PAID": "Уплаченные налоги и сборы",
    "OFFENCE": "Налоговое правонарушение",
    "FINE_AMOUNT": "Сумма штрафа",
    "TOTAL": "Итого",
    "OSN": "Общая система налогообложения",
    "GENERAL": "Общая система налогообложения",
    "USN": "Упрощённая система налогообложения",
    "USN_INCOME": "УСН «Доходы»",
    "USN_INCOME_EXPENSE": "УСН «Доходы минус расходы»",
    "ESHN": "Единый сельскохозяйственный налог",
    "PATENT": "Патентная система налогообложения",
    "NPD": "Налог на профессиональный доход",
    "ENVD": "Единый налог на вменённый доход",
}

_PUBLIC_VALUE_KEYS = {
    "amount": "Сумма",
    "code": "Код",
    "count": "Количество",
    "currency": "Валюта",
    "fine_amount": "Сумма штрафа",
    "name": "Наименование",
    "regime": "Система налогообложения",
    "regime_code": "Система налогообложения",
    "regime_codes": "Системы налогообложения",
    "status": "Статус",
    "total": "Итого",
    "value": "Значение",
}

_HIDDEN_PUBLIC_VALUE_KEYS = {"person_ref", "related_person_ref"}

_FIELD_LABELS = {
    "activity": "Вид деятельности",
    "additional_activity": "Дополнительный вид деятельности",
    "aggregate": "Сводные сведения",
    "capital": "Уставный капитал",
    "case": "Запись",
    "connection": "Связь",
    "contact": "Контакт",
    "employee_count": "Численность сотрудников",
    "event": "Событие",
    "finance": "Финансовый показатель",
    "founder": "Учредитель или участник",
    "legal_form": "Организационно-правовая форма",
    "license": "Лицензия",
    "manager": "Руководитель",
    "primary_activity": "Основной вид деятельности",
    "regime": "Система налогообложения",
    "registered_address": "Юридический адрес",
    "registration_date": "Дата регистрации",
    "status": "Статус организации",
    "tax_debt": "Налоговая задолженность",
    "tax_offence": "Налоговое правонарушение",
    "tax_payment": "Уплаченные налоги и сборы",
}

_TECHNICAL_TOKEN = re.compile(r"^[A-Z][A-Z0-9_ -]{1,80}$")


def _normalized(value: object | None) -> str:
    return str(value or "").strip().replace("ё", "е").upper()


def company_status_label(value: object | None) -> str:
    """Return an explicit legal-status label without optimistic fallback."""

    raw = str(value or "").strip()
    normalized = _normalized(value)
    if normalized in _LEGAL_STATUS_LABELS:
        return _LEGAL_STATUS_LABELS[normalized]
    if normalized in _UNKNOWN_LEGAL_STATUSES or _TECHNICAL_TOKEN.fullmatch(raw):
        return "Статус организации не определён"
    return "Статус организации не определён"


def company_status_tone(value: object | None) -> str:
    normalized = _normalized(value)
    if normalized in {"ACTIVE", "ДЕЙСТВУЕТ", "ДЕЙСТВУЮЩАЯ"}:
        return "active"
    if normalized in _UNKNOWN_LEGAL_STATUSES or normalized not in _LEGAL_STATUS_LABELS:
        return "unknown"
    return "attention"


def ui_label(value: object | None, fallback: str = "Не определено") -> str:
    """Translate known contract enums; fail closed for unknown technical tokens."""

    raw = str(value or "").strip()
    if not raw:
        return fallback
    normalized = _normalized(raw)
    if normalized in _UI_LABELS:
        return _UI_LABELS[normalized]
    if _TECHNICAL_TOKEN.fullmatch(raw):
        return fallback
    return raw


def field_label(value: object | None, fallback: str = "Сведения") -> str:
    """Render contract field labels without leaking snake-case identifiers."""

    raw = str(value or "").strip()
    if not raw:
        return fallback
    mapped = _FIELD_LABELS.get(raw.casefold())
    if mapped:
        return mapped
    if re.fullmatch(r"[a-z][a-z0-9_]*", raw) or _TECHNICAL_TOKEN.fullmatch(raw):
        return fallback
    return raw


def public_value(value: object | None, *, field_key: str | None = None) -> str:
    if value is None or value == "":
        return "Не указано"
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
            if normalized_key in _HIDDEN_PUBLIC_VALUE_KEYS:
                continue
            label = _PUBLIC_VALUE_KEYS.get(normalized_key, "Сведения")
            parts.append(
                f"{label}: {public_value(child, field_key=normalized_key)}"
            )
        return " · ".join(parts) or "Не указано"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "; ".join(
            public_value(item, field_key=field_key) for item in value
        ) or "Не указано"
    return ui_label(value, fallback="Не определено")


def monitoring_status_label(value: object | None) -> str:
    return {
        "ACTIVE": "Мониторинг включён",
        "PAUSED": "Мониторинг приостановлен",
        "NOT_ACTIVE": "Мониторинг не подключён",
    }.get(_normalized(value), "Статус мониторинга не определён")


def record_status_label(value: object | None) -> str:
    return {
        "ACTIVE": "Активно",
        "SUSPENDED": "Приостановлено",
        "REVOKED": "Доступ отозван",
        "DISABLED": "Отключено",
        "PENDING": "Ожидает",
        "EXPIRED": "Срок истёк",
    }.get(_normalized(value), "Статус не определён")


def relation_status_label(value: object | None) -> str:
    return {
        "CURRENT": "Текущая связь",
        "HISTORICAL": "Историческая связь",
    }.get(_normalized(value), "Период связи не определён")


def freshness_label(value: object | None) -> str:
    return {
        "CURRENT": "Актуальные сведения",
        "STALE": "Требуют обновления",
        "UNKNOWN": "Актуальность не определена",
    }.get(_normalized(value), "Актуальность не определена")
