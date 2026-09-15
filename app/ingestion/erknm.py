from __future__ import annotations

from datetime import date
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.postgres import get_session
from app.models.erknm import ErknmInspection
from app.models.source import DataSet


DATASET_CODE = "erknm_inspections"
SOURCE_URL = "https://proverki.gov.ru/portal/public-open-data"


def local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[-1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[-1]
    return tag


def normalize_inn(value: str | None) -> str | None:
    value = (value or "").strip()
    if value.isdigit() and len(value) in {10, 12}:
        return value
    return None


def normalize_ogrn(value: str | None) -> str | None:
    value = (value or "").strip()
    if value.isdigit() and len(value) in {13, 15}:
        return value
    return None


def parse_iso_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None

    # В открытых данных ожидаем ISO date, но допускаем timestamp.
    candidate = value[:10]
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        return None


def _attrs(element: ET.Element | None) -> dict[str, str]:
    if element is None:
        return {}
    return {
        local_name(key): str(value).strip()
        for key, value in element.attrib.items()
        if str(value).strip()
    }


def _direct_child(
    element: ET.Element | None,
    name: str,
) -> ET.Element | None:
    if element is None:
        return None
    for child in element:
        if local_name(child.tag) == name:
            return child
    return None


def _direct_children(
    element: ET.Element | None,
    name: str,
) -> list[ET.Element]:
    if element is None:
        return []
    return [
        child
        for child in element
        if local_name(child.tag) == name
    ]


def _first_descendant(
    element: ET.Element | None,
    name: str,
) -> ET.Element | None:
    if element is None:
        return None
    for child in element.iter():
        if child is element:
            continue
        if local_name(child.tag) == name:
            return child
    return None


def _text_or_value(element: ET.Element | None) -> str | None:
    if element is None:
        return None

    attrs = _attrs(element)
    for key in ("VALUE", "NAME", "TEXT"):
        value = attrs.get(key)
        if value:
            return value

    text = " ".join((element.text or "").split())
    return text or None


def _parse_object(element: ET.Element) -> dict:
    attrs = _attrs(element)
    result = {"attributes": attrs}

    for xml_name, result_name in (
        ("OBJECT_TYPE", "type"),
        ("OBJECT_KIND", "kind"),
        ("OBJECT_SUB_KIND", "sub_kind"),
        ("RISK_CATEGORY", "risk_category"),
    ):
        value = _text_or_value(_direct_child(element, xml_name))
        if value:
            result[result_name] = value

    if attrs.get("ADDRESS"):
        result["address"] = attrs["ADDRESS"]

    return result


def _parse_inspector(element: ET.Element) -> dict:
    attrs = _attrs(element)
    result = {"attributes": attrs}
    position = _text_or_value(_direct_child(element, "INSPECTOR_POSITION"))
    if position:
        result["position"] = position
    return result


def _parse_reason(element: ET.Element) -> dict:
    attrs = _attrs(element)
    result = {"attributes": attrs}
    reason_type = _direct_child(element, "REASON_TYPE")
    if reason_type is not None:
        result["reason_type_attributes"] = _attrs(reason_type)
        value = _text_or_value(reason_type)
        if value:
            result["reason_type"] = value
    return result


def parse_inspection_element(
    element: ET.Element,
    *,
    data_date: date,
    period_year: int,
    period_month: int,
) -> dict | None:
    attrs = _attrs(element)
    erpid = attrs.get("ERPID")
    if not erpid:
        return None

    subject = _direct_child(element, "SUBJECT")
    subject_attrs = _attrs(subject)

    okveds = []
    if subject is not None:
        for okved in _direct_children(subject, "OKVEDS"):
            item = _attrs(okved)
            if item:
                okveds.append(item)

    objects = [
        _parse_object(item)
        for item in _direct_children(element, "OBJECT")
    ]
    inspectors = [
        _parse_inspector(item)
        for item in _direct_children(element, "INSPECTOR")
    ]

    reason_risk = _direct_child(element, "REASON_RISK")
    reasons = [
        _parse_reason(item)
        for item in _direct_children(reason_risk, "REASON")
    ]

    first_object = objects[0] if objects else {}
    first_reason = reasons[0] if reasons else {}
    reason_attrs = first_reason.get("attributes") or {}

    warning_info = _direct_child(element, "WARNING_INFO")
    actions_info = _direct_child(element, "ACTIONS_INFO")

    result_element = _first_descendant(actions_info, "RESULT")
    caption_element = _direct_child(warning_info, "CAPTION")

    prosecutor = _direct_child(element, "PROSECUTOR_OFFICE")
    kind_control = _direct_child(element, "KIND_CONTROL")
    kind_knm = _direct_child(element, "KIND_KNM")
    kno = _direct_child(element, "KNO_ORGANIZATION")
    place_element = _direct_child(element, "PLACE")

    return {
        "data_date": data_date,
        "period_year": period_year,
        "period_month": period_month,
        "erpid": erpid,
        "classification": attrs.get("CLASSIFICATION"),
        "creation_source": attrs.get("CREATION_SOURCE"),
        "status": attrs.get("STATUS"),
        "status_key": attrs.get("STATUS_KEY"),
        "control_level": attrs.get("KO_LEVEL"),
        "supervision_name": attrs.get("SUPERVISION_ID"),
        "start_date": parse_iso_date(attrs.get("START_DATE")),
        "end_date": parse_iso_date(attrs.get("END_DATE")),
        "prosecutor_office": _text_or_value(prosecutor),
        "kind_control": _text_or_value(kind_control),
        "kind_knm": _text_or_value(kind_knm),
        "kno_organization": _text_or_value(kno),
        "subject_inn": normalize_inn(subject_attrs.get("INN")),
        "subject_ogrn": normalize_ogrn(subject_attrs.get("OGRN")),
        "subject_name": subject_attrs.get("NAME"),
        "subject_type": subject_attrs.get("TYPE"),
        "subject_guid": subject_attrs.get("GUID"),
        "msp_code": subject_attrs.get("MSP_CODE"),
        "place": _text_or_value(place_element),
        "object_address": first_object.get("address"),
        "object_type": first_object.get("type"),
        "object_kind": first_object.get("kind"),
        "object_sub_kind": first_object.get("sub_kind"),
        "risk_category": first_object.get("risk_category"),
        "reason_text": reason_attrs.get("TEXT"),
        "warning_caption": _text_or_value(caption_element),
        "result_text": _text_or_value(result_element),
        "inspection_attributes": attrs or None,
        "subject_attributes": subject_attrs or None,
        "okveds": okveds or None,
        "objects": objects or None,
        "inspectors": inspectors or None,
        "reasons": reasons or None,
    }


def select_xml_member(archive: ZipFile, member: str | None = None) -> str:
    members = [
        info
        for info in archive.infolist()
        if not info.is_dir() and info.filename.lower().endswith(".xml")
    ]

    if member is not None:
        names = {item.filename for item in members}
        if member not in names:
            raise ValueError(f"XML member не найден в ZIP: {member}")
        return member

    if not members:
        raise ValueError("В ZIP не найден XML")

    return max(members, key=lambda item: item.file_size).filename


def iter_erknm_records(
    zip_path: str | Path,
    *,
    data_date: date,
    period_year: int,
    period_month: int,
    member: str | None = None,
    limit: int | None = None,
):
    path = Path(zip_path)
    if not path.is_file():
        raise FileNotFoundError(f"ZIP не найден: {path}")

    if period_month < 1 or period_month > 12:
        raise ValueError("period_month должен быть от 1 до 12")

    yielded = 0

    with ZipFile(path) as archive:
        selected = select_xml_member(archive, member=member)
        with archive.open(selected) as stream:
            context = ET.iterparse(stream, events=("start", "end"))
            root = None

            for event, element in context:
                if event == "start" and root is None:
                    root = element
                    continue

                if event != "end" or local_name(element.tag) != "INSPECTION":
                    continue

                record = parse_inspection_element(
                    element,
                    data_date=data_date,
                    period_year=period_year,
                    period_month=period_month,
                )

                element.clear()
                if root is not None:
                    root.clear()

                if record is None:
                    continue

                yield record
                yielded += 1

                if limit is not None and yielded >= limit:
                    return


def get_dataset_id(dataset_code: str = DATASET_CODE) -> int:
    session = get_session()
    try:
        dataset_id = (
            session.execute(
                select(DataSet.id).where(DataSet.code == dataset_code)
            )
            .scalar_one_or_none()
        )
        if dataset_id is None:
            raise ValueError(f"Dataset не найден: {dataset_code}")
        return dataset_id
    finally:
        session.close()


def _upsert_erknm_batch(session, records: list[dict], dataset_id: int) -> dict:
    input_count = len(records)
    if not records:
        return {"processed": 0, "inserted": 0, "updated": 0, "skipped": 0}

    unique = {}
    for record in records:
        unique[record["erpid"]] = record

    clean_records = list(unique.values())
    skipped = input_count - len(clean_records)
    keys = [record["erpid"] for record in clean_records]

    existing = set(
        session.execute(
            select(ErknmInspection.erpid).where(
                ErknmInspection.dataset_id == dataset_id,
                ErknmInspection.erpid.in_(keys),
            )
        ).scalars().all()
    )

    values = [
        {"dataset_id": dataset_id, **record}
        for record in clean_records
    ]

    insert_stmt = pg_insert(ErknmInspection).values(values)

    update_fields = (
        "data_date",
        "period_year",
        "period_month",
        "classification",
        "creation_source",
        "status",
        "status_key",
        "control_level",
        "supervision_name",
        "start_date",
        "end_date",
        "prosecutor_office",
        "kind_control",
        "kind_knm",
        "kno_organization",
        "subject_inn",
        "subject_ogrn",
        "subject_name",
        "subject_type",
        "subject_guid",
        "msp_code",
        "place",
        "object_address",
        "object_type",
        "object_kind",
        "object_sub_kind",
        "risk_category",
        "reason_text",
        "warning_caption",
        "result_text",
        "inspection_attributes",
        "subject_attributes",
        "okveds",
        "objects",
        "inspectors",
        "reasons",
    )

    statement = insert_stmt.on_conflict_do_update(
        constraint="uq_erknm_inspections_dataset_erpid",
        set_={
            **{
                field: getattr(insert_stmt.excluded, field)
                for field in update_fields
            },
            "updated_at": func.now(),
        },
    )

    session.execute(statement)
    session.flush()

    updated = len(existing)
    inserted = len(clean_records) - updated

    return {
        "processed": input_count,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def process_erknm_batch(records: list[dict], dataset_id: int) -> dict:
    session = get_session()
    try:
        result = _upsert_erknm_batch(session, records, dataset_id)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def import_erknm_zip(
    zip_path: str | Path,
    *,
    data_date: date,
    period_year: int,
    period_month: int,
    batch_size: int = 1000,
    dataset_id: int | None = None,
) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size должен быть > 0")

    if dataset_id is None:
        dataset_id = get_dataset_id()

    totals = {
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "batches": 0,
        "with_inn": 0,
        "with_ogrn": 0,
        "without_identifiers": 0,
        "legal_subjects": 0,
        "ip_subjects": 0,
        "other_subjects": 0,
        "dataset_id": dataset_id,
    }

    batch: list[dict] = []

    def flush_batch() -> None:
        if not batch:
            return
        result = process_erknm_batch(list(batch), dataset_id)
        totals["inserted"] += result["inserted"]
        totals["updated"] += result["updated"]
        totals["skipped"] += result["skipped"]
        totals["batches"] += 1
        batch.clear()

    for record in iter_erknm_records(
        zip_path,
        data_date=data_date,
        period_year=period_year,
        period_month=period_month,
    ):
        totals["processed"] += 1

        if record["subject_inn"]:
            totals["with_inn"] += 1
        if record["subject_ogrn"]:
            totals["with_ogrn"] += 1
        if not record["subject_inn"] and not record["subject_ogrn"]:
            totals["without_identifiers"] += 1

        subject_type = (record["subject_type"] or "").strip().upper()
        if subject_type == "ЮЛ":
            totals["legal_subjects"] += 1
        elif subject_type == "ИП":
            totals["ip_subjects"] += 1
        else:
            totals["other_subjects"] += 1

        batch.append(record)
        if len(batch) >= batch_size:
            flush_batch()

    flush_batch()
    return totals
