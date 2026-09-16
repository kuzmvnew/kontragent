from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, insert, select

from app.database.postgres import get_session
from app.models.cbr_warning_list import CbrWarningListEntry
from app.models.source import DataSet


DATASET_CODE = "cbr_warning_list"
DEFAULT_BATCH_SIZE = 1000


_KEY_RE = re.compile(r"[^a-zа-я0-9]+", re.IGNORECASE)
_SITE_SPLIT_RE = re.compile(r"[\n;,]+")


def _normalize_key(value: Any) -> str:
    return _KEY_RE.sub("", str(value or "").strip().lower())


def _pick(row: dict, *aliases):
    normalized = {
        _normalize_key(key): value
        for key, value in row.items()
    }

    for alias in aliases:
        key = _normalize_key(alias)
        if key in normalized:
            return normalized[key]

    return None


def parse_cbr_date(value) -> date | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    candidates = (
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
    )

    for format_string in candidates:
        try:
            return datetime.strptime(
                text[:10],
                format_string,
            ).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date()
    except ValueError:
        return None


def normalize_inn(value) -> str | None:
    digits = "".join(
        symbol
        for symbol in str(value or "")
        if symbol.isdigit()
    )

    if len(digits) not in {10, 12}:
        return None

    return digits


def _to_text(value) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _normalize_sites(value) -> list[str]:
    if value is None:
        return []

    if isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = _SITE_SPLIT_RE.split(str(value))

    result = []

    for item in values:
        if isinstance(item, dict):
            item = (
                _pick(item, "site", "url", "value", "name")
                or json.dumps(item, ensure_ascii=False, sort_keys=True)
            )

        text = _to_text(item)
        if text and text not in result:
            result.append(text)

    return result


def _normalize_named_list(value) -> list[dict]:
    if value is None:
        return []

    if isinstance(value, dict):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        text = _to_text(value)
        return [{"name": text}] if text else []

    result = []

    for item in values:
        if isinstance(item, dict):
            item_id = _pick(item, "id", "code", "okato")
            name = _pick(
                item,
                "signRus",
                "name",
                "reg_name",
                "text",
                "value",
            )

            normalized = {}

            if item_id is not None:
                normalized["id"] = str(item_id).strip()

            if _to_text(name):
                normalized["name"] = _to_text(name)

            if not normalized:
                normalized["raw"] = item

            if normalized not in result:
                result.append(normalized)

        else:
            text = _to_text(item)
            if text:
                normalized = {"name": text}
                if normalized not in result:
                    result.append(normalized)

    return result


def _extract_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise ValueError("CBR warning list JSON должен быть object или array")

    for key in (
        "Data",
        "data",
        "Items",
        "items",
        "Records",
        "records",
        "Info",
        "info",
        "List",
        "list",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    # Защита от небольших изменений оболочки JSON:
    # выбираем самый большой массив объектов первого уровня.
    candidates = [
        value
        for value in payload.values()
        if isinstance(value, list)
        and all(isinstance(item, dict) for item in value)
    ]

    if candidates:
        return max(candidates, key=len)

    # Если API вернул одну запись без массива.
    if any(
        _pick(payload, alias) is not None
        for alias in ("id", "nameOrg", "inn", "ИНН")
    ):
        return [payload]

    raise ValueError("Не удалось определить список записей в JSON Банка России")


def _stable_record_id(row: dict) -> str:
    source_id = _pick(row, "id", "cbr_id", "record_id", "ID")

    if source_id is not None and str(source_id).strip():
        return str(source_id).strip()

    digest = hashlib.sha256(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    return f"sha256:{digest}"


def normalize_warning_record(
    row: dict,
    *,
    data_date: date,
) -> dict:
    if not isinstance(row, dict):
        raise ValueError("Запись CBR warning list должна быть object")

    signs = _pick(
        row,
        "Signs",
        "signs",
        "sign",
        "signRus",
        "Признаки",
    )

    regions = _pick(
        row,
        "Regions",
        "regions",
        "region",
        "regionName",
        "Регион",
    )

    record = {
        "data_date": data_date,
        "cbr_id": _stable_record_id(row),
        "inn": normalize_inn(
            _pick(row, "inn", "ИНН", "innOrg", "taxpayerInn")
        ),
        "name": _to_text(
            _pick(
                row,
                "nameOrg",
                "name",
                "organizationName",
                "Наименование",
            )
        ),
        "entry_date": parse_cbr_date(
            _pick(row, "dt", "entryDate", "date", "ДатаВнесения")
        ),
        "update_date": parse_cbr_date(
            _pick(
                row,
                "dateUpdate",
                "updateDate",
                "lastUpdateDate",
                "ДатаОбновления",
            )
        ),
        "address": _to_text(
            _pick(row, "addr", "address", "Адрес")
        ),
        "sites": _normalize_sites(
            _pick(row, "site", "sites", "website", "Сайт")
        ),
        "signs": _normalize_named_list(signs),
        "regions": _normalize_named_list(regions),
        "additional_info": _to_text(
            _pick(row, "info", "additionalInfo", "ДопИнформация")
        ),
        "liquidation_status": _to_text(
            _pick(row, "isLikvid", "liquidationStatus", "liquidated")
        ),
        "comment": _to_text(
            _pick(row, "comment", "Комментарий")
        ),
        "org_type": _to_text(
            _pick(row, "OrgType", "orgType", "type", "ТипЗаписи")
        ),
        "raw_payload": row,
    }

    if not (record["name"] or record["inn"] or record["sites"]):
        raise ValueError("Запись CBR warning list не содержит идентифицирующих полей")

    return record


def parse_cbr_warning_payload(
    payload,
    *,
    data_date: date,
) -> dict:
    source_rows = _extract_rows(payload)

    unique = {}
    rejected = 0

    for row in source_rows:
        try:
            record = normalize_warning_record(
                row,
                data_date=data_date,
            )
        except (TypeError, ValueError):
            rejected += 1
            continue

        unique[record["cbr_id"]] = record

    duplicate_records = (
        len(source_rows)
        - rejected
        - len(unique)
    )

    records = list(unique.values())

    return {
        "records": records,
        "source_records": len(source_rows),
        "imported_records": len(records),
        "rejected_records": rejected,
        "duplicate_records": duplicate_records,
        "with_inn": sum(
            1 for record in records if record["inn"] is not None
        ),
        "without_inn": sum(
            1 for record in records if record["inn"] is None
        ),
    }


def get_dataset_id(
    dataset_code: str = DATASET_CODE,
) -> int:
    session = get_session()

    try:
        dataset_id = (
            session.execute(
                select(DataSet.id)
                .where(DataSet.code == dataset_code)
            )
            .scalar_one_or_none()
        )

        if dataset_id is None:
            raise RuntimeError(
                f"Dataset {dataset_code} не зарегистрирован. "
                "Запусти scripts.init_sources."
            )

        return dataset_id
    finally:
        session.close()


def replace_cbr_warning_list(
    records: list[dict],
    *,
    dataset_id: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """
    Атомарно заменяет текущий snapshot предупредительного списка.

    Старые строки удаляются и новый snapshot вставляется в одной
    транзакции, поэтому карточка не видит частично обновлённый список.
    """

    if batch_size < 1:
        raise ValueError("batch_size должен быть > 0")

    if not records:
        raise ValueError("Нельзя публиковать пустой CBR warning list snapshot")

    session = get_session()

    try:
        session.execute(
            delete(CbrWarningListEntry)
            .where(CbrWarningListEntry.dataset_id == dataset_id)
        )

        inserted = 0

        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]

            values = [
                {
                    "dataset_id": dataset_id,
                    **record,
                }
                for record in batch
            ]

            if values:
                session.execute(
                    insert(CbrWarningListEntry),
                    values,
                )
                inserted += len(values)

        session.commit()

        return {
            "inserted": inserted,
            "dataset_id": dataset_id,
        }

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
