import csv
from dataclasses import replace
from datetime import date, datetime, time, timezone
from html import unescape
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import (
    insert as pg_insert,
)

from app.database.postgres import get_session
from app.models.disqualified_person import (
    DisqualifiedPersonSnapshot,
)
from app.models.company import Company
from app.models.source import DataSet


DATASET_CODE = "fns_disqualified"

SOURCE_URL = (
    "https://www.nalog.gov.ru/"
    "opendata/"
    "7707329152-registerdisqualified/"
)

EXPECTED_HEADER = [
    "G1",
    "G2",
    "G3",
    "G4",
    "G5",
    "G6",
    "G7",
    "G8",
    "G9",
    "G10",
    "G11",
    "G12",
    "G13",
    "G14",
]


def parse_fns_date(value):
    value = (value or "").strip()

    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%d.%m.%Y",
        ).date()
    except ValueError:
        return None


def normalize_organization_inn(value):
    value = (value or "").strip()

    if not value:
        return None

    if (
        value.isdigit()
        and len(value) == 10
    ):
        return value

    return None


def parse_disqualified_row(
    row,
    data_date: date,
):
    if len(row) != 14:
        return None

    register_number = row[0].strip()
    full_name = row[1].strip()

    if not register_number:
        return None

    if not full_name:
        return None

    return {
        "data_date": data_date,
        "register_number": register_number,
        "full_name": full_name,
        "birth_date": parse_fns_date(
            row[2]
        ),
        "birth_place": (
            row[3].strip()
            or None
        ),
        "organization_name": (
            row[4].strip()
            or None
        ),
        "organization_inn": (
            normalize_organization_inn(
                row[5]
            )
        ),
        "position": (
            row[6].strip()
            or None
        ),
        "offence_article": (
            row[7].strip()
            or None
        ),
        "protocol_authority": (
            row[8].strip()
            or None
        ),
        "judge_name": (
            row[9].strip()
            or None
        ),
        "judge_position": (
            row[10].strip()
            or None
        ),
        "disqualification_term": (
            row[11].strip()
            or None
        ),
        "start_date": parse_fns_date(
            row[12]
        ),
        "end_date": parse_fns_date(
            row[13]
        ),
    }


def iter_disqualified_records(
    csv_path,
    data_date: date,
    limit=None,
):
    path = Path(csv_path)

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.reader(file)

        header = next(
            reader,
            None,
        )

        if header != EXPECTED_HEADER:
            raise ValueError(
                "Unexpected FNS "
                "disqualified CSV header: "
                f"{header}"
            )

        yielded = 0

        for row in reader:

            record = (
                parse_disqualified_row(
                    row=row,
                    data_date=data_date,
                )
            )

            if record is None:
                continue

            yield record

            yielded += 1

            if (
                limit is not None
                and yielded >= limit
            ):
                break



def get_dataset_id(
    dataset_code: str = DATASET_CODE,
):
    session = get_session()

    try:
        dataset_id = (
            session.execute(
                select(DataSet.id)
                .where(
                    DataSet.code
                    == dataset_code
                )
            )
            .scalar_one_or_none()
        )

        if dataset_id is None:
            raise ValueError(
                "Dataset не найден: "
                f"{dataset_code}"
            )

        return dataset_id

    finally:
        session.close()


def _upsert_disqualified_batch(
    session,
    records,
    dataset_id,
):
    """
    UPSERT одного batch в уже открытую
    транзакцию.

    Функция сама не делает commit,
    поэтому её можно безопасно тестировать
    с последующим rollback.
    """

    input_count = len(records)

    if not records:
        return {
            "processed": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
        }

    unique_records = {}

    for record in records:
        key = (
            record["data_date"],
            record["register_number"],
        )

        unique_records[key] = record

    clean_records = list(
        unique_records.values()
    )

    skipped = (
        input_count
        - len(clean_records)
    )

    keys = [
        (
            record["data_date"],
            record["register_number"],
        )
        for record in clean_records
    ]

    existing_rows = (
        session.execute(
            select(
                DisqualifiedPersonSnapshot.data_date,
                DisqualifiedPersonSnapshot.register_number,
            )
            .where(
                DisqualifiedPersonSnapshot.dataset_id
                == dataset_id,
                tuple_(
                    DisqualifiedPersonSnapshot.data_date,
                    DisqualifiedPersonSnapshot.register_number,
                ).in_(keys),
            )
        )
        .all()
    )

    existing = {
        (
            row.data_date,
            row.register_number,
        )
        for row in existing_rows
    }

    values = [
        {
            "dataset_id": dataset_id,
            **record,
        }
        for record in clean_records
    ]

    insert_stmt = pg_insert(
        DisqualifiedPersonSnapshot
    ).values(
        values
    )

    update_fields = (
        "full_name",
        "birth_date",
        "birth_place",
        "organization_name",
        "organization_inn",
        "position",
        "offence_article",
        "protocol_authority",
        "judge_name",
        "judge_position",
        "disqualification_term",
        "start_date",
        "end_date",
    )

    update_values = {
        field: getattr(
            insert_stmt.excluded,
            field,
        )
        for field in update_fields
    }

    update_values["updated_at"] = (
        func.now()
    )

    statement = (
        insert_stmt
        .on_conflict_do_update(
            constraint=(
                "uq_disqualified_person_"
                "dataset_date_register"
            ),
            set_=update_values,
        )
    )

    session.execute(
        statement
    )

    session.flush()

    updated = len(
        existing
    )

    inserted = (
        len(clean_records)
        - updated
    )

    return {
        "processed": input_count,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def process_disqualified_batch(
    records,
    dataset_id,
):
    """
    UPSERT batch с собственной транзакцией.
    """

    session = get_session()

    try:
        result = (
            _upsert_disqualified_batch(
                session=session,
                records=records,
                dataset_id=dataset_id,
            )
        )

        session.commit()

        return result

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def import_fns_disqualified_csv(
    csv_path,
    data_date: date,
    batch_size: int = 1000,
    limit=None,
    dataset_id=None,
):
    """
    Потоковый импорт CSV ФНС.

    Повторный запуск безопасен:
    используется UPSERT по
    dataset_id + data_date + register_number.
    """

    path = Path(
        csv_path
    )

    if not path.is_file():
        raise FileNotFoundError(
            f"CSV не найден: {path}"
        )

    if batch_size < 1:
        raise ValueError(
            "batch_size должен быть > 0"
        )

    if dataset_id is None:
        dataset_id = get_dataset_id()

    totals = {
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "batches": 0,
        "with_org_inn": 0,
        "without_org_inn": 0,
        "dataset_id": dataset_id,
    }

    batch = []

    def flush_batch():
        if not batch:
            return

        result = (
            process_disqualified_batch(
                records=list(batch),
                dataset_id=dataset_id,
            )
        )

        totals["inserted"] += (
            result["inserted"]
        )

        totals["updated"] += (
            result["updated"]
        )

        totals["skipped"] += (
            result["skipped"]
        )

        totals["batches"] += 1

        batch.clear()

    for record in (
        iter_disqualified_records(
            csv_path=path,
            data_date=data_date,
            limit=limit,
        )
    ):
        totals["processed"] += 1

        if record["organization_inn"]:
            totals[
                "with_org_inn"
            ] += 1
        else:
            totals[
                "without_org_inn"
            ] += 1

        batch.append(
            record
        )

        if (
            len(batch)
            >= batch_size
        ):
            flush_batch()

    flush_batch()

    return totals


# ---------------------------------------------------------------------------
# Worker Foundation adapter
# ---------------------------------------------------------------------------

SOURCE_ID = DATASET_CODE
SOURCE_PAGE_URL = SOURCE_URL
SOURCE_PATH = "7707329152-registerdisqualified"
HANDLER_VERSION = "disqualified-official-v1"


def _worker_spec():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    return FnsBulkSourceSpec(
        source_id=SOURCE_ID,
        dataset_code=DATASET_CODE,
        source_page_url=SOURCE_PAGE_URL,
        source_path=SOURCE_PATH,
        handler_version=HANDLER_VERSION,
        kind="disqualified",
        api_projection="disqualified_check",
        card_projection="company_card.disqualified",
        check_frequency="daily",
    )


def _release_date_from_csv_url(url: str) -> date:
    name = Path(urlparse(url).path).name
    match = re.fullmatch(
        r"data-(?P<release_date>\d{8})-structure-\d{8}\.csv",
        name,
        flags=re.I,
    )
    if match is None:
        from app.worker.errors import SchemaMismatchError

        raise SchemaMismatchError(
            "official disqualified artifact filename is ambiguous"
        )
    try:
        return datetime.strptime(match.group("release_date"), "%Y%m%d").date()
    except ValueError as error:
        from app.worker.errors import SchemaMismatchError

        raise SchemaMismatchError(
            "official disqualified artifact has invalid release date"
        ) from error


def discover_fns_disqualified_release(*, now=None, fetch=None):
    from app.ingestion.fns_bulk_worker import (
        FnsRelease,
        _parse_date,
        _read_url,
        _utc,
        utc_now,
    )
    from app.worker.errors import InvalidDataError, SchemaMismatchError

    observed_at = _utc(now or utc_now())
    raw, _headers = (fetch or _read_url)(SOURCE_PAGE_URL)
    html = unescape(raw.decode("utf-8", errors="replace"))
    urls = re.findall(r'href=["\'](https://[^"\']+)["\']', html, flags=re.I)
    prefix = f"/opendata/{SOURCE_PATH}/"
    artifacts = []
    structures = {}
    for url in urls:
        if not urlparse(url).path.startswith(prefix):
            continue
        artifact_match = re.search(
            r"/data-(\d{8})-structure-(\d{8})\.csv$", url, re.I
        )
        if artifact_match:
            try:
                released = datetime.strptime(artifact_match.group(1), "%Y%m%d").date()
            except ValueError as error:
                raise SchemaMismatchError(
                    "official disqualified artifact has invalid release date"
                ) from error
            artifacts.append((released, artifact_match.group(2), url))
            continue
        structure_match = re.search(r"/structure-(\d{8})\.csv$", url, re.I)
        if structure_match:
            structures.setdefault(structure_match.group(1), set()).add(url)
    if not artifacts:
        raise SchemaMismatchError(
            "official disqualified passport has no current data/structure CSV"
        )
    newest_date = max(item[0] for item in artifacts)
    newest = {(item[1], item[2]) for item in artifacts if item[0] == newest_date}
    if len(newest) != 1:
        raise SchemaMismatchError(
            "official disqualified passport current artifact is ambiguous"
        )
    structure_token, artifact_url = newest.pop()
    exact_structures = structures.get(structure_token, set())
    if len(exact_structures) != 1:
        raise SchemaMismatchError(
            "official disqualified passport structure is missing or ambiguous"
        )
    structure_url = next(iter(exact_structures))
    for candidate in (artifact_url, structure_url):
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.nalog.gov.ru",
            "data.nalog.ru",
            "file.nalog.ru",
        }:
            raise InvalidDataError(
                f"official passport points to unapproved URL: {candidate}"
            )
    valid_match = re.search(
        r'property=["\']dc:valid["\'][^>]*content=["\']([^"\']+)',
        html,
        flags=re.I,
    )
    actual_until = _parse_date(valid_match.group(1)) if valid_match else None
    source_date = _release_date_from_csv_url(artifact_url)
    return FnsRelease(
        source_page_url=SOURCE_PAGE_URL,
        artifact_url=artifact_url,
        xsd_url=structure_url,
        source_data_date=source_date,
        actual_until=actual_until,
        discovered_at=observed_at,
        provenance=f"Обновление данных набора {source_date.isoformat()}",
    )


def _minimal_disqualified_record(record):
    """Drop birth/place, judge and protocol fields before normalized storage."""

    return {
        "data_date": record["data_date"],
        "register_number": record["register_number"],
        "full_name": record["full_name"],
        "organization_name": record.get("organization_name"),
        "organization_inn": record.get("organization_inn"),
        "position": record.get("position"),
        "offence_article": record.get("offence_article"),
        "disqualification_term": record.get("disqualification_term"),
        "start_date": record.get("start_date"),
        "end_date": record.get("end_date"),
        "matching_state": (
            "organization_inn_exact_candidate"
            if record.get("organization_inn")
            else "no_organization_inn_name_match_prohibited"
        ),
    }


def _normalize_disqualified_csv(csv_path: Path, *, data_date: date):
    from app.ingestion.fns_bulk_worker import (
        _hash_file,
        _json_record,
        _persist_download,
    )
    from app.worker.errors import SchemaMismatchError

    descriptor, name = tempfile.mkstemp(
        prefix="normalized-", suffix=".jsonl.tmp", dir=csv_path.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    records_seen = with_org_inn = without_org_inn = 0
    register_numbers = set()
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source, temporary.open(
            "w", encoding="utf-8"
        ) as output:
            reader = csv.reader(source)
            if next(reader, None) != EXPECTED_HEADER:
                raise SchemaMismatchError(
                    "unexpected official FNS disqualified CSV header"
                )
            for line_number, row in enumerate(reader, 2):
                record = parse_disqualified_row(row=row, data_date=data_date)
                if record is None:
                    raise SchemaMismatchError(
                        f"invalid official disqualified CSV row: {line_number}"
                    )
                register_number = record["register_number"]
                if register_number in register_numbers:
                    raise SchemaMismatchError(
                        "duplicate register number in official disqualified CSV"
                    )
                register_numbers.add(register_number)
                minimal = _minimal_disqualified_record(record)
                output.write(_json_record(minimal) + "\n")
                records_seen += 1
                if minimal["organization_inn"]:
                    with_org_inn += 1
                else:
                    without_org_inn += 1
        if records_seen == 0:
            raise SchemaMismatchError("official disqualified CSV is empty")
        checksum, size = _hash_file(temporary)
        normalized = csv_path.parent / f"normalized-{checksum}.jsonl"
        _persist_download(temporary, normalized, checksum=checksum)
        return normalized, checksum, {
            "records_seen": records_seen,
            "records_valid": records_seen,
            "records_rejected": 0,
            "with_organization_inn": with_org_inn,
            "without_organization_inn": without_org_inn,
            "normalized_size": size,
            "source_data_date": data_date.isoformat(),
            "personal_data_policy": "minimum-company-card-v1",
            "name_only_matching_used": False,
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def fns_disqualified_worker_handler(context):
    from app.ingestion.fns_bulk_worker import (
        release_from_metadata,
        stage_release,
        utc_now,
    )
    from app.worker.contracts import (
        ExecutionCounters,
        HandlerResult,
        RawArtifactReference,
        StagingResult,
        ValidationResult,
    )
    from app.worker.errors import InvalidDataError

    metadata = context.schedule_metadata
    release = release_from_metadata(metadata)
    if release.source_page_url != SOURCE_PAGE_URL:
        raise InvalidDataError("FNS source passport URL differs from handler pin")
    if metadata.get("check_only"):
        return HandlerResult(
            checksum_metadata={
                "release_identity": release.identity,
                "check_only": True,
                "replay_snapshot": bool(metadata.get("replay_snapshot")),
            },
            counters=ExecutionCounters(),
        )
    context.ensure_active(now=utc_now())
    raw_root_text = str(metadata.get("raw_root") or "").strip()
    if not raw_root_text:
        raise InvalidDataError("raw_root is required")
    csv_path, structure_path, manifest = stage_release(
        _worker_spec(), release, raw_root=Path(raw_root_text)
    )
    if structure_path.stat().st_size == 0:
        raise InvalidDataError("official disqualified structure CSV is empty")
    context.heartbeat()
    normalized, normalized_checksum, counters = _normalize_disqualified_csv(
        csv_path, data_date=release.source_data_date
    )
    execution = ExecutionCounters(
        records_seen=counters["records_seen"],
        records_written=counters["records_valid"],
    )
    context.report_counters(execution)
    return HandlerResult(
        raw_artifacts=(
            RawArtifactReference(
                artifact_reference=csv_path.as_uri(),
                checksum=manifest["artifact_sha256"],
                manifest=manifest,
            ),
        ),
        staging_result=StagingResult(
            staging_pointer=normalized.as_uri(),
            checksum=normalized_checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={
                    "release_identity": release.identity,
                    "source_data_date": release.source_data_date.isoformat(),
                    "coverage": counters,
                },
            ),
            metadata={
                "source_id": SOURCE_ID,
                "dataset_code": DATASET_CODE,
                "artifact_url": release.artifact_url,
                "structure_url": release.xsd_url,
                "api_projection": _worker_spec().api_projection,
                "card_projection": _worker_spec().card_projection,
            },
        ),
        checksum_metadata={
            "artifact_sha256": manifest["artifact_sha256"],
            "structure_sha256": manifest["xsd_sha256"],
            "normalized_sha256": normalized_checksum,
            "release_identity": release.identity,
        },
        counters=execution,
    )


def _disqualified_signature(value):
    return (
        value["full_name"],
        value.get("organization_name"),
        value.get("organization_inn"),
        value.get("position"),
        value.get("offence_article"),
        value.get("disqualification_term"),
        value.get("start_date"),
        value.get("end_date"),
    )


def _disqualified_values(row, *, dataset_id):
    def parsed(name):
        return date.fromisoformat(row[name]) if row.get(name) else None

    return {
        "dataset_id": dataset_id,
        "data_date": parsed("data_date"),
        "register_number": row["register_number"],
        "full_name": row["full_name"],
        # The product needs the company relationship and legal context only.
        # More sensitive official columns remain in immutable RAW, not domain DB.
        "birth_date": None,
        "birth_place": None,
        "organization_name": row.get("organization_name"),
        "organization_inn": row.get("organization_inn"),
        "position": row.get("position"),
        "offence_article": row.get("offence_article"),
        "protocol_authority": None,
        "judge_name": None,
        "judge_position": None,
        "disqualification_term": row.get("disqualification_term"),
        "start_date": parsed("start_date"),
        "end_date": parsed("end_date"),
    }


def _project_disqualified(
    session,
    *,
    dataset,
    staging_path,
    replay,
):
    from app.ingestion.fns_bulk_worker import _iter_jsonl

    matched = unmatched = quarantined = inserted = 0
    matched_company_ids = set()
    signatures = {}
    for batch in _iter_jsonl(staging_path):
        inns = {
            str(row["organization_inn"])
            for row in batch
            if row.get("organization_inn")
        }
        companies = dict(
            session.execute(
                select(Company.inn, Company.id).where(
                    Company.inn.in_(inns), Company.entity_type == "legal"
                )
            ).all()
        )
        values = []
        for row in batch:
            inn = row.get("organization_inn")
            if not inn:
                quarantined += 1
                continue
            company_id = companies.get(str(inn))
            if company_id is None:
                unmatched += 1
                continue
            matched += 1
            matched_company_ids.add(int(company_id))
            value = _disqualified_values(row, dataset_id=dataset.id)
            signatures[value["register_number"]] = _disqualified_signature(value)
            values.append(value)
        if not values:
            continue
        statement = pg_insert(DisqualifiedPersonSnapshot).values(values)
        if replay:
            statement = statement.on_conflict_do_nothing(
                constraint="uq_disqualified_person_dataset_date_register"
            )
        inserted += len(
            session.scalars(
                statement.returning(DisqualifiedPersonSnapshot.id)
            ).all()
        )
    return {
        "matched": matched,
        "unmatched": unmatched,
        "quarantined": quarantined,
        "source_records": matched + unmatched + quarantined,
        "inserted": inserted,
        "matched_company_ids": matched_company_ids,
        "signatures": signatures,
    }


def publish_fns_disqualified_worker_result(session, claim, result):
    from app.contracts.data_readiness import AutoUpdateStatus
    from app.ingestion.fns_bulk_worker import (
        _accepted_replay_path,
        _apply_successful_check,
        _claim_actual_until,
        _file_path,
        utc_now,
    )
    from app.worker.contracts import ExecutionCounters, SourceChangeSummary
    from app.worker.errors import InvalidDataError

    spec = _worker_spec()
    dataset = session.scalar(
        select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")
    now = utc_now()
    actual_until = _claim_actual_until(claim)
    source_date = date.fromisoformat(str(claim.schedule_metadata["source_data_date"]))
    previous_source_date = dataset.last_data_date

    if claim.schedule_metadata.get("check_only"):
        replay_path = _accepted_replay_path(session, claim=claim, spec=spec)
        projected = _project_disqualified(
            session,
            dataset=dataset,
            staging_path=replay_path,
            replay=True,
        )
        published = int(
            session.scalar(
                select(func.count())
                .select_from(DisqualifiedPersonSnapshot)
                .where(
                    DisqualifiedPersonSnapshot.dataset_id == dataset.id,
                    DisqualifiedPersonSnapshot.data_date == source_date,
                )
            )
            or 0
        )
        status = _apply_successful_check(
            dataset,
            actual_until=actual_until,
            now=now,
            check_interval=spec.check_interval,
        )
        dataset.record_count = published
        summary = SourceChangeSummary(
            matched_companies=len(projected["matched_company_ids"]),
            new_facts=projected["inserted"],
            changed_facts=0,
            removed_or_expired_facts=0,
            unchanged_facts=published - projected["inserted"],
            replayed_facts=projected["inserted"],
            quarantined_records=projected["quarantined"],
            source_records=projected["source_records"],
            source_data_date=source_date,
            previous_source_data_date=previous_source_date,
        )
        coverage = dict(dataset.coverage or {})
        coverage.update(
            {
                "last_replay": {
                    "checked_at": now.isoformat(),
                    "matched": projected["matched"],
                    "unmatched": projected["unmatched"],
                    "new_facts": projected["inserted"],
                },
                "published_facts": published,
                "matching_states": {
                    "organization_inn_exact": projected["matched"],
                    "organization_inn_unmatched": projected["unmatched"],
                    "no_organization_inn_name_match_prohibited": projected[
                        "quarantined"
                    ],
                    "name_only_matching_used": False,
                },
                "change_summary": summary.as_dict(),
            }
        )
        dataset.coverage = coverage
        return replace(
            result,
            staging_result=None,
            checksum_metadata={
                **result.checksum_metadata,
                "freshness": status.value,
                "official_actual_until": actual_until.isoformat()
                if actual_until
                else None,
            },
            counters=ExecutionCounters(
                records_seen=projected["source_records"],
                records_written=projected["inserted"],
                records_published=projected["inserted"],
            ),
            change_summary=summary,
        )

    if result.staging_result is None:
        raise InvalidDataError("disqualified publisher requires normalized staging")
    staging_path = _file_path(result.staging_result.staging_pointer)
    previous = {
        row.register_number: _disqualified_signature(row.__dict__)
        for row in session.scalars(
            select(DisqualifiedPersonSnapshot).where(
                DisqualifiedPersonSnapshot.dataset_id == dataset.id,
                DisqualifiedPersonSnapshot.data_date == previous_source_date,
            )
        )
    } if previous_source_date else {}
    session.execute(
        delete(DisqualifiedPersonSnapshot).where(
            DisqualifiedPersonSnapshot.dataset_id == dataset.id
        )
    )
    projected = _project_disqualified(
        session,
        dataset=dataset,
        staging_path=staging_path,
        replay=False,
    )
    published = projected["inserted"]
    current = projected["signatures"]
    shared = set(previous) & set(current)
    changed = sum(previous[key] != current[key] for key in shared)
    unchanged = len(shared) - changed
    new = len(set(current) - set(previous))
    removed = len(set(previous) - set(current))
    dataset.enabled = True
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.last_success_at = now
    dataset.last_data_date = source_date
    dataset.source_as_of = datetime.combine(source_date, time.min, tzinfo=timezone.utc)
    dataset.retrieved_at = now
    dataset.published_at = now
    dataset.record_count = published
    status = _apply_successful_check(
        dataset,
        actual_until=actual_until,
        now=now,
        check_interval=spec.check_interval,
    )
    summary = SourceChangeSummary(
        matched_companies=len(projected["matched_company_ids"]),
        new_facts=new,
        changed_facts=changed,
        removed_or_expired_facts=removed,
        unchanged_facts=unchanged,
        replayed_facts=0,
        quarantined_records=projected["quarantined"],
        source_records=projected["source_records"],
        source_data_date=source_date,
        previous_source_data_date=previous_source_date,
        unavailable_reasons=(
            {"previous_source_data_date": "first accepted publication"}
            if previous_source_date is None
            else {}
        ),
    )
    dataset.coverage = {
        "source_records": projected["source_records"],
        "matched": projected["matched"],
        "unmatched": projected["unmatched"],
        "published_facts": published,
        "risk_summary_candidate_companies": len(projected["matched_company_ids"]),
        "api_projection": spec.api_projection,
        "card_projection": spec.card_projection,
        "release_identity": claim.schedule_metadata.get("release_identity"),
        "personal_data_policy": "minimum-company-card-v1",
        "matching_states": {
            "organization_inn_exact": projected["matched"],
            "organization_inn_unmatched": projected["unmatched"],
            "no_organization_inn_name_match_prohibited": projected["quarantined"],
            "name_only_matching_used": False,
        },
        "change_summary": summary.as_dict(),
    }
    validation = replace(
        result.staging_result.validation,
        metadata={
            **result.staging_result.validation.metadata,
            "matched": projected["matched"],
            "unmatched": projected["unmatched"],
            "quarantined": projected["quarantined"],
            "published_facts": published,
            "api_projection": spec.api_projection,
            "card_projection": spec.card_projection,
            "freshness": status.value,
            "official_actual_until": actual_until.isoformat()
            if actual_until
            else None,
            "personal_data_policy": "minimum-company-card-v1",
            "name_only_matching_used": False,
        },
    )
    return replace(
        result,
        staging_result=replace(result.staging_result, validation=validation),
        counters=ExecutionCounters(
            records_seen=projected["source_records"],
            records_written=result.counters.records_written if result.counters else 0,
            records_published=published,
        ),
        change_summary=summary,
    )


def register_fns_disqualified_worker(session, registry):
    from app.ingestion.fns_bulk_worker import register_bulk_handler

    return register_bulk_handler(
        session,
        registry,
        spec=_worker_spec(),
        handler=fns_disqualified_worker_handler,
        publisher=publish_fns_disqualified_worker_result,
    )


def schedule_fns_disqualified_check(session, *, raw_root, now=None, fetch=None):
    from app.ingestion.fns_bulk_worker import enqueue_bulk_release
    from app.models.worker import WorkerPublicationState

    release = discover_fns_disqualified_release(now=now, fetch=fetch)
    state = session.get(WorkerPublicationState, SOURCE_ID)
    validation = dict(state.validation_metadata or {}) if state else {}
    same_release = (
        (validation.get("validation") or {}).get("release_identity")
        == release.identity
    )
    return enqueue_bulk_release(
        session,
        spec=_worker_spec(),
        release=release,
        raw_root=Path(raw_root),
        check_only=same_release,
        replay_pointer=state.active_pointer if same_release and state else None,
        replay_checksum=str(validation.get("checksum") or "")
        if same_release
        else None,
        scheduled_for=release.discovered_at.date(),
    )
