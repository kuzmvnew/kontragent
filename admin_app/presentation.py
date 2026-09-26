"""Russian owner-facing presentation helpers for the private admin console."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

HOME_TIMEZONE_NAME = os.getenv("ADMIN_TIMEZONE", "Europe/Moscow")
HOME_TIMEZONE = ZoneInfo(HOME_TIMEZONE_NAME)

STATUS_LABELS = {
    "OPERATIONAL": "РАБОТАЕТ",
    "FIRST RUN": "ПЕРВЫЙ ЗАПУСК",
    "CHECK PENDING": "ОЖИДАЕТ ПРОВЕРКИ",
    "UPDATING": "ОБНОВЛЯЕТСЯ",
    "STALE": "ДАННЫЕ УСТАРЕЛИ",
    "ERROR": "ОШИБКА",
    "SOURCE BLOCKED": "ПРОБЛЕМА У ИСТОЧНИКА",
    "ACCESS REQUIRED": "НУЖЕН ДОСТУП",
    "DISABLED": "ОТКЛЮЧЁН",
    "NOT CONFIGURED": "ЕЩЁ НЕ ПОДКЛЮЧЁН",
    "PLANNED": "ЗАПЛАНИРОВАН",
    "CURRENT": "АКТУАЛЬНЫ",
    "UNAVAILABLE": "НЕДОСТУПНЫ",
    "WAITING_SOURCE": "ОЖИДАЕМ ИСТОЧНИК",
    "WAITING SOURCE": "ОЖИДАЕМ ИСТОЧНИК",
    "AUTO_HEAL_RUNNING": "ИДЁТ ВОССТАНОВЛЕНИЕ",
    "RETRY_SCHEDULED": "ПОВТОР ЗАПЛАНИРОВАН",
    "RESOLVED": "ВОССТАНОВЛЕН",
    "REVIEW_REQUIRED": "НУЖНА ПРОВЕРКА",
    "AWAITING_ENGINEERING_REVIEW": "НУЖНА ПРОВЕРКА",
    "AGENT_UNAVAILABLE": "АВТОИСПРАВЛЕНИЕ КОДА НЕДОСТУПНО",
    "AGENT UNAVAILABLE": "АВТОИСПРАВЛЕНИЕ КОДА НЕДОСТУПНО",
    "REVIEW REQUIRED": "НУЖНА ПРОВЕРКА",
    "AUTO_REPAIR_EXHAUSTED": "АВТОВОССТАНОВЛЕНИЕ ОСТАНОВЛЕНО",
    "OPEN": "ОТКРЫТ",
    "CANCELLED": "ОТМЕНЁН",
    "RUNNING": "ВЫПОЛНЯЕТСЯ",
    "SUCCEEDED": "УСПЕШНО",
    "FAILED": "ОШИБКА",
    "TIMED_OUT": "ПРЕВЫШЕНО ВРЕМЯ",
    "INTERRUPTED": "ПРЕРВАН",
    "QUEUED": "В ОЧЕРЕДИ",
    "PENDING": "ОЖИДАЕТ ВЫПОЛНЕНИЯ",
    "SUCCESS": "УСПЕШНО",
    "NOOP": "ДЕЙСТВИЕ НЕ ТРЕБУЕТСЯ",
    "SOURCE_STILL_INVALID": "ИСТОЧНИК ВСЁ ЕЩЁ СОДЕРЖИТ ОШИБКУ",
    "ON": "ВКЛЮЧЕНО",
    "OFF": "ОТКЛЮЧЕНО",
    "CLOSED": "ЗАКРЫТ",
    "EXHAUSTED": "ОСТАНОВЛЕНО",
    "HEALTHY": "РАБОТАЕТ",
    "PASS": "ПРОЙДЕНА",
    "FAIL": "ОШИБКА",
    "NOT RUN": "НЕ ВЫПОЛНЯЛАСЬ",
    "CRITICAL": "КРИТИЧЕСКАЯ",
    "HIGH": "ВЫСОКАЯ",
    "MEDIUM": "СРЕДНЯЯ",
    "LOW": "НИЗКАЯ",
}

OWNER_LABELS = {
    "SOURCE_OWNED": "Официальный источник",
    "OUR_INFRASTRUCTURE": "Инфраструктура HOME WORKER",
    "OUR_CODE": "Код обработки NEXT Company",
    "ACCESS_REQUIRED": "Требуется доступ владельца",
    "UNKNOWN": "Причина уточняется",
}

CATEGORY_LABELS = {
    "SOURCE_SCHEMA_VIOLATION": "Официальный файл не проходит проверку схемы",
    "SOURCE_INVALID_ARTIFACT": "Официальный файл повреждён или имеет неверный формат",
    "SOURCE_STALE": "Официальный источник давно не обновлялся",
    "SOURCE_UNAVAILABLE": "Официальный источник недоступен",
    "SOURCE_ACCESS_REQUIRED": "Для источника требуется настроить доступ",
    "WORKER_NOT_RUNNING": "Фоновый worker не запущен",
    "LEASE_STUCK": "Задача worker зависла",
    "PARSER_ERROR": "Ошибка в обработчике данных",
    "NORMALIZATION_ERROR": "Ошибка нормализации данных",
    "PUBLICATION_ERROR": "Ошибка публикации данных",
    "TEMPORARY_NETWORK": "Временная сетевая ошибка",
    "HTTP_5XX": "Временная ошибка сервера источника",
    "TIMEOUT": "Источник не ответил вовремя",
    "DNS_FAILURE": "Не удалось найти адрес источника",
    "RATE_LIMIT": "Источник временно ограничил частоту запросов",
    "DATABASE_UNAVAILABLE": "PostgreSQL недоступен",
    "DATABASE_LOCK": "Операция заблокирована PostgreSQL",
    "CHECKSUM_MISMATCH": "Контрольная сумма файла не совпала",
    "DISK_PRESSURE": "Недостаточно свободного места",
    "UNKNOWN": "Причина пока не определена",
}

ACTION_LABELS = {
    "DETECTED": "Инцидент обнаружен",
    "CLASSIFIED": "Причина классифицирована",
    "MANUAL_SOURCE_RECHECK_REQUESTED": "Запрошена внеочередная проверка",
    "SOURCE_REDISCOVERY": "Проверка официального источника",
    "SOURCE_RECHECK": "Результат проверки источника",
    "SOURCE_RECHECK_SCHEDULED": "Следующая проверка запланирована",
    "RETRY_SCHEDULED": "Повтор запланирован",
    "RETRY_EXECUTED": "Повтор запущен",
    "WORKER_RESTART": "Worker перезапущен",
    "LEASE_RECOVERY": "Зависшая задача освобождена",
    "RECOVERED": "Работа восстановлена",
    "EXHAUSTED": "Автовосстановление остановлено",
    "AUTO_REPAIR_PAUSED": "Автоматика приостановлена",
    "AUTO_REPAIR_RESUMED": "Автоматика возобновлена",
    "AUTO_HEAL_REQUESTED": "Запущено восстановление",
    "ENGINEERING_REPAIR_REQUESTED": "Запрошено инженерное исправление",
    "REPAIR_PACKAGE_CREATED": "Подготовлен диагностический пакет",
    "AGENT_STARTED": "Запуск coding-agent",
    "CANCELLED": "Ожидающее действие отменено",
}


def _datetime(value: Any) -> datetime | None:
    local_wall_clock = False
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?::\d{2})?", text)
            if not match:
                return None
            parsed = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
            local_wall_clock = True
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HOME_TIMEZONE if local_wall_clock else timezone.utc)
    return parsed.astimezone(HOME_TIMEZONE)


def format_datetime(value: Any, empty: str = "—") -> str:
    parsed = _datetime(value)
    return parsed.strftime("%d.%m.%Y %H:%M") if parsed else empty


def format_date(value: Any, empty: str = "—") -> str:
    if isinstance(value, datetime):
        return value.astimezone(HOME_TIMEZONE).strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
        except ValueError:
            return empty
    return empty


def status_label(value: Any) -> str:
    text = str(value or "").strip()
    return STATUS_LABELS.get(text.upper(), text.replace("_", " ") or "Нет данных")


def owner_label(value: Any) -> str:
    text = str(value or "UNKNOWN")
    return OWNER_LABELS.get(text, text)


def category_label(value: Any) -> str:
    text = str(value or "UNKNOWN")
    return CATEGORY_LABELS.get(text, text)


def action_label(value: Any) -> str:
    text = str(value or "")
    return ACTION_LABELS.get(text, text.replace("_", " "))
