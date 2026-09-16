from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.roszdrav import (
    RoszdravClinicalOrganizationEntry,
    RoszdravLicenseEntry,
    RoszdravMedicalDeviceCheck,
    RoszdravUnifiedLicenseCheck,
)
from app.models.source import DataSet, IngestionRun
from app.services.roszdrav_registry_service import (
    CLINICAL_ORG_DATASET,
    MEDICAL_DEVICE_DATASET,
    UNIFIED_LICENSE_DATASET,
)


_INN_RE = re.compile(r"[0-9]{10}|[0-9]{12}")


def _date(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _text(element, name):
    child = element.find(name)
    value = child.text if child is not None else None
    value = str(value or "").strip()
    return value or None


def _element_payload(element) -> dict:
    result = {}
    for child in element:
        value = _element_payload(child) if list(child) else str(child.text or "").strip()
        if child.tag in result:
            if not isinstance(result[child.tag], list):
                result[child.tag] = [result[child.tag]]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def parse_license_xml(content: bytes, *, category: str, data_date: date) -> dict:
    records = []
    rejected = 0
    keys = set()
    duplicates = 0
    try:
        iterator = ET.iterparse(io.BytesIO(content), events=("end",))
        for _, element in iterator:
            if element.tag.rsplit("}", 1)[-1] != "licenses":
                continue
            raw = _element_payload(element)
            inn = str(raw.get("inn") or "").strip()
            number = str(raw.get("number") or "").strip()
            if not _INN_RE.fullmatch(inn) or not number:
                rejected += 1
                element.clear()
                continue
            work_places = []
            work_parent = element.find("work_address_list")
            if work_parent is not None:
                for place in work_parent.findall("address_place"):
                    works = place.find("works")
                    work_places.append({
                        "address": _text(place, "address"),
                        "region": _text(place, "region"),
                        "city": _text(place, "city"),
                        "fias_id": _text(place, "code_fias"),
                        "works": [str(item.text or "").strip() for item in (works.findall("work") if works is not None else []) if str(item.text or "").strip()],
                    })
            record_key = hashlib.sha256(
                json.dumps([category, inn, number, raw], ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if record_key in keys:
                duplicates += 1
            else:
                keys.add(record_key)
                records.append({
                    "data_date": data_date, "record_key": record_key,
                    "category": category, "inn": inn,
                    "ogrn": str(raw.get("ogrn") or "").strip() or None,
                    "license_number": number,
                    "licensee_name": str(raw.get("full_name_licensee") or "").strip() or None,
                    "authority_name": str(raw.get("name") or "").strip() or None,
                    "activity_type": str(raw.get("activity_type") or "").strip() or None,
                    "legal_form": str(raw.get("form") or "").strip() or None,
                    "address": str(raw.get("address") or "").strip() or None,
                    "work_places": work_places,
                    "decision_date": _date(raw.get("date_order")),
                    "start_date": _date(raw.get("date_register") or raw.get("date")),
                    "end_date": _date(raw.get("date_end")),
                    "termination_info": str(raw.get("termination") or "").strip() or None,
                    "termination_date": _date(raw.get("date_termination")),
                    "suspension_info": str(raw.get("information_suspension_resumption") or "").strip() or None,
                    "cancellation_info": str(raw.get("information_cancellation") or "").strip() or None,
                    "raw_payload": raw,
                })
            element.clear()
    except ET.ParseError as error:
        raise ValueError("Некорректный XML реестра лицензий Росздравнадзора") from error
    source_records = len(records) + duplicates + rejected
    return {"records": records, "source_records": source_records, "imported_records": len(records), "duplicate_records": duplicates, "rejected_records": rejected}


def parse_clinical_csv(content: bytes, *, data_date: date) -> dict:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    required = {"date", "name", "inn", "address", "phone", "email"}
    if set(reader.fieldnames or []) != required:
        raise ValueError("Неожиданная структура CSV клинических организаций")
    records, keys = [], set()
    rejected = duplicates = 0
    for raw in reader:
        inn = str(raw.get("inn") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not _INN_RE.fullmatch(inn) or not name:
            rejected += 1
            continue
        key = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if key in keys:
            duplicates += 1
            continue
        keys.add(key)
        records.append({
            "data_date": data_date, "record_key": key, "inn": inn,
            "included_at": _date(raw.get("date")), "name": name,
            "address": str(raw.get("address") or "").strip() or None,
            "phone": str(raw.get("phone") or "").strip() or None,
            "email": str(raw.get("email") or "").strip() or None,
            "raw_payload": raw,
        })
    return {"records": records, "source_records": len(records) + duplicates + rejected, "imported_records": len(records), "duplicate_records": duplicates, "rejected_records": rejected}


def validate_complete_snapshot(parsed: dict) -> None:
    if not parsed["records"]:
        raise ValueError("Пустой snapshot Росздравнадзора не публикуется")
    if parsed["rejected_records"]:
        raise ValueError("Snapshot содержит отклонённые записи")
    if parsed["source_records"] != parsed["imported_records"] + parsed["duplicate_records"]:
        raise ValueError("Контроль количества snapshot не пройден")


def _publish_snapshot_in_session(
    session, *, dataset_code: str, parsed: dict, model, data_date: date,
    checksum: str | None, source_file_name: str | None, details: dict | None,
    batch_size: int,
) -> dict:
    dataset = session.execute(
        select(DataSet).where(DataSet.code == dataset_code).with_for_update()
    ).scalar_one()
    session.execute(delete(model).where(model.dataset_id == dataset.id))
    for start in range(0, len(parsed["records"]), batch_size):
        session.execute(
            insert(model),
            [
                {"dataset_id": dataset.id, **row}
                for row in parsed["records"][start : start + batch_size]
            ],
        )
    now = datetime.now(timezone.utc)
    session.add(IngestionRun(
        dataset_id=dataset.id, status="success", started_at=now,
        finished_at=now, data_date=data_date, source_file_name=source_file_name,
        source_url=dataset.source_url, file_checksum=checksum,
        rows_read=parsed["source_records"], rows_inserted=parsed["imported_records"],
        rows_updated=0, rows_skipped=parsed["duplicate_records"], errors_count=0,
        details={
            "rejected_records": parsed["rejected_records"],
            "complete_snapshot": True,
            **(details or {}),
        },
    ))
    dataset.last_data_date = data_date
    dataset.last_success_at = now
    return {"dataset_id": dataset.id, "inserted": parsed["imported_records"]}


def publish_snapshots(snapshots: list[dict], *, batch_size: int = 1000) -> dict:
    if not snapshots:
        raise ValueError("Не переданы snapshot для публикации")
    for snapshot in snapshots:
        validate_complete_snapshot(snapshot["parsed"])
    session = get_session()
    try:
        results = {}
        for snapshot in snapshots:
            results[snapshot["dataset_code"]] = _publish_snapshot_in_session(
                session,
                dataset_code=snapshot["dataset_code"],
                parsed=snapshot["parsed"],
                model=snapshot["model"],
                data_date=snapshot["data_date"],
                checksum=snapshot.get("checksum"),
                source_file_name=snapshot.get("source_file_name"),
                details=snapshot.get("details"),
                batch_size=batch_size,
            )
        session.commit()
        return results
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def publish_snapshot(*, dataset_code: str, parsed: dict, model, data_date: date, checksum: str | None = None, source_file_name: str | None = None, details: dict | None = None, batch_size: int = 1000) -> dict:
    return publish_snapshots([{
        "dataset_code": dataset_code,
        "parsed": parsed,
        "model": model,
        "data_date": data_date,
        "checksum": checksum,
        "source_file_name": source_file_name,
        "details": details,
    }], batch_size=batch_size)[dataset_code]


def _save_check(model, dataset_code: str, key_fields: dict, values: dict):
    session = get_session()
    try:
        dataset_id = session.execute(select(DataSet.id).where(DataSet.code == dataset_code)).scalar_one()
        now = datetime.now(timezone.utc)
        all_values = {"dataset_id": dataset_id, **key_fields, **values, "checked_at": now, "updated_at": now}
        constraint = "uq_roszdrav_unified_license_check" if model is RoszdravUnifiedLicenseCheck else "uq_roszdrav_medical_device_check"
        statement = insert(model).values(**all_values).on_conflict_do_update(
            constraint=constraint,
            set_={key: value for key, value in all_values.items() if key not in {"dataset_id", *key_fields}},
        )
        session.execute(statement)
        session.commit()
        return session.execute(select(model).where(model.dataset_id == dataset_id, *(getattr(model, key) == value for key, value in key_fields.items()))).scalar_one()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def save_unified_license_check(*, inn: str, request_date: date, **values):
    return _save_check(RoszdravUnifiedLicenseCheck, UNIFIED_LICENSE_DATASET, {"inn": inn, "request_date": request_date}, values)


def save_medical_device_check(*, registration_number: str, request_date: date, **values):
    return _save_check(RoszdravMedicalDeviceCheck, MEDICAL_DEVICE_DATASET, {"registration_number": registration_number, "request_date": request_date}, values)
