"""Official GIRBO REST API adapter for accounting-report revisions.

The transport follows the FNS ``REST_API.zip`` instruction (version 1.0.3):
OAuth2 password grant, ``/api/v1/years``, paginated ``/api/v1/files/``
discovery, then download by the returned file token. Credentials and file
tokens never enter durable job metadata or manifests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from lxml import etree
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.company import Company
from app.models.girbo import GirboAccountingReport
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.worker.contracts import (
    ExecutionCounters,
    HandlerContext,
    HandlerResult,
    RawArtifactReference,
    SourceChangeSummary,
    StagingResult,
    ValidationResult,
)
from app.worker.errors import (
    AccessRequiredError,
    HandlerNotRegisteredError,
    InvalidDataError,
    SchemaMismatchError,
    WorkerNetworkError,
)
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


SOURCE_ID = DATASET_CODE = "girbo_accounting"
HANDLER_VERSION = "girbo-accounting-api-v1"
CHECK_INTERVAL = timedelta(days=1)
OFFICIAL_BASE_URL = "https://api-bo.nalog.gov.ru"
REQUIRED_ACCESS = ("GIRBO_USERNAME", "GIRBO_PASSWORD", "GIRBO_OAUTH_BASIC")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def access_state(environ: dict[str, str] | None = None) -> tuple[bool, tuple[str, ...]]:
    values = environ if environ is not None else os.environ
    missing = tuple(
        name for name in REQUIRED_ACCESS if not str(values.get(name) or "").strip()
    )
    return not missing, missing


def require_access(environ: dict[str, str] | None = None) -> None:
    ready, missing = access_state(environ)
    if not ready:
        raise AccessRequiredError(
            "GIRBO OAuth2 access is not configured; missing: "
            + ", ".join(sorted(missing))
        )


class GirboApiProvider:
    def __init__(self, environ: dict[str, str] | None = None):
        self.environ = environ if environ is not None else os.environ
        self._access_token: str | None = None

    def _response(
        self, request: Request, *, expected_content: str | None
    ) -> tuple[bytes, dict[str, str]]:
        try:
            with urlopen(request, timeout=180) as response:
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                content_type = headers.get("content-type", "").lower()
                if expected_content and expected_content not in content_type:
                    raise SchemaMismatchError(
                        "GIRBO response content type differs: "
                        + (content_type or "missing")
                    )
                return response.read(), headers
        except HTTPError as error:
            if error.code in {401, 403}:
                raise AccessRequiredError(
                    "GIRBO API rejected configured access"
                ) from error
            raise WorkerNetworkError(f"GIRBO API HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise WorkerNetworkError("GIRBO API request failed") from error

    def _token(self) -> str:
        require_access(self.environ)
        if self._access_token:
            return self._access_token
        boundary = f"nextcompany-{secrets.token_hex(12)}"
        parts = []
        for name, value in (
            ("username", self.environ["GIRBO_USERNAME"]),
            ("password", self.environ["GIRBO_PASSWORD"]),
            ("grant_type", "password"),
        ):
            parts.append(
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n"
            )
        body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        authorization = self.environ["GIRBO_OAUTH_BASIC"].strip()
        if not authorization.lower().startswith("basic "):
            authorization = f"Basic {authorization}"
        request = Request(
            f"{OFFICIAL_BASE_URL}/oauth/token",
            data=body,
            method="POST",
            headers={
                "Authorization": authorization,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        content, _headers = self._response(
            request, expected_content="json"
        )
        try:
            payload = json.loads(content)
            token = str(payload["access_token"]).strip()
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise SchemaMismatchError(
                "GIRBO OAuth2 response has no access_token"
            ) from error
        if not token:
            raise SchemaMismatchError("GIRBO OAuth2 access_token is empty")
        self._access_token = token
        return token

    def _get(
        self, path: str, *, expected_content: str | None
    ) -> tuple[bytes, dict[str, str]]:
        if not path.startswith("/"):
            raise InvalidDataError("GIRBO API path must be absolute")
        request = Request(
            f"{OFFICIAL_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json" if expected_content == "json" else "*/*",
            },
        )
        return self._response(request, expected_content=expected_content)

    def available_years(self) -> tuple[int, ...]:
        content, _headers = self._get(
            "/api/v1/years", expected_content="json"
        )
        try:
            values = json.loads(content)
            years = tuple(sorted({int(value) for value in values}))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise SchemaMismatchError("GIRBO years response is invalid") from error
        if not years:
            raise SchemaMismatchError("GIRBO reports no available years")
        return years

    def list_files(self, *, inn: str, year: int) -> tuple[dict[str, Any], ...]:
        page = 0
        documents: list[dict[str, Any]] = []
        while True:
            query = urlencode(
                {
                    "period": year,
                    "periodType": "TWELVE_MONTHS",
                    "inn": inn,
                    "reportType": "BFO_TKS",
                    "fileType": "BFO",
                    "size": 100,
                    "sort": "id,asc",
                    "page": page,
                }
            )
            content, _headers = self._get(
                f"/api/v1/files/?{query}", expected_content="json"
            )
            try:
                payload = json.loads(content)
                rows = payload["content"]
                total_pages = int(payload.get("totalPages") or 1)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise SchemaMismatchError(
                    "GIRBO file-list response is invalid"
                ) from error
            if not isinstance(rows, list):
                raise SchemaMismatchError(
                    "GIRBO file-list content is not an array"
                )
            for row in rows:
                if not isinstance(row, dict):
                    raise SchemaMismatchError(
                        "GIRBO file-list entry is not an object"
                    )
                if str(row.get("inn") or "") != inn:
                    raise SchemaMismatchError(
                        "GIRBO file-list INN differs from request"
                    )
                if not str(row.get("token") or "").strip():
                    raise SchemaMismatchError(
                        "GIRBO file-list entry has no download token"
                    )
                documents.append(row)
            page += 1
            if page >= total_pages:
                break
        return tuple(documents)

    def download_file(self, token: str) -> tuple[bytes, dict[str, str]]:
        # The token is used only in memory and is intentionally absent from
        # durable schedules/manifests/errors.
        return self._get(
            f"/api/v1/files/{token}", expected_content=None
        )


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern).date()
        except ValueError:
            pass
    raise SchemaMismatchError("GIRBO response contains an invalid date")


def _decimal(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value).replace(" ", "").replace(",", ".")))
    except InvalidOperation as error:
        raise SchemaMismatchError(
            "GIRBO report contains a non-numeric value"
        ) from error


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element(root: etree._Element, *names: str) -> etree._Element | None:
    wanted = set(names)
    return next(
        (item for item in root.iter() if _local_name(item.tag) in wanted), None
    )


def _sum_value(root: etree._Element, *names: str) -> str | None:
    item = _element(root, *names)
    return _decimal(item.get("СумОтч")) if item is not None else None


def parse_girbo_xml(
    content: bytes,
    *,
    requested_inn: str,
    metadata: dict[str, Any],
    raw_checksum: str,
    member_name: str | None = None,
) -> dict[str, Any]:
    """Normalize current FNS 0710099/0710096 XML."""

    try:
        root = etree.fromstring(
            content,
            parser=etree.XMLParser(
                resolve_entities=False, no_network=True, huge_tree=True
            ),
        )
    except etree.XMLSyntaxError as error:
        raise SchemaMismatchError(
            "GIRBO BFO file is not well-formed XML"
        ) from error
    observed_inns = {
        str(value)
        for item in root.iter()
        for key, value in item.attrib.items()
        if "ИНН" in key
        and str(value).isdigit()
        and len(str(value)) in {10, 12}
    }
    if observed_inns and requested_inn not in observed_inns:
        raise SchemaMismatchError("GIRBO XML INN differs from requested INN")
    statement_values: dict[str, dict[str, str | None]] = {}
    for item in root.iter():
        if item.get("СумОтч") is None:
            continue
        statement_values[_local_name(item.tag)] = {
            "current": _decimal(item.get("СумОтч")),
            "previous": _decimal(item.get("СумПрдщ")),
            "previous_two": _decimal(item.get("СумПрдшв")),
        }
    if not statement_values:
        raise SchemaMismatchError(
            "GIRBO structured report has no accounting values"
        )
    year_value = metadata.get("period")
    source_date = _parse_date(metadata.get("uploadDate"))
    if not str(year_value or "").isdigit() or source_date is None:
        raise SchemaMismatchError(
            "GIRBO document period/upload date is invalid"
        )
    revenue = _sum_value(root, "Выруч", "Выручка")
    expenses = _sum_value(root, "СебестПрод", "Расходы")
    profit_loss = _sum_value(root, "ЧистПрибУб", "ЧистПриб")
    assets = _sum_value(root, "Актив")
    equity = _sum_value(root, "Капитал", "ЦелФинанс")
    passive = _sum_value(root, "Пассив")
    liabilities = None
    if passive is not None and equity is not None:
        liabilities = str(Decimal(passive) - Decimal(equity))
    logical_id = ":".join(
        (
            requested_inn,
            str(year_value),
            str(metadata.get("periodType") or "TWELVE_MONTHS"),
            str(metadata.get("reportType") or "BFO_TKS"),
            str(metadata.get("fileType") or "BFO"),
        )
    )
    document_node = _element(root, "Документ")
    statement_values["_document"] = {
        "api_id": str(metadata.get("id") or ""),
        "file_name": str(metadata.get("fileName") or ""),
        "format_version": str(root.get("ВерсФорм") or ""),
        "unit_code": str(
            (document_node.get("ОКЕИ") if document_node is not None else "") or ""
        ),
        "archive_member": member_name or "",
    }
    return {
        "inn": requested_inn,
        "report_id": logical_id,
        "reporting_year": int(year_value),
        "publication_date": source_date.isoformat(),
        "correction_date": None,
        "source_data_date": source_date.isoformat(),
        "statement_values": statement_values,
        "revenue": revenue,
        "expenses": expenses,
        "profit_loss": profit_loss,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "raw_checksum": raw_checksum,
    }


def _xml_members(content: bytes) -> tuple[tuple[str | None, bytes], ...]:
    if content.startswith(b"PK"):
        try:
            with ZipFile(BytesIO(content)) as archive:
                members = tuple(
                    (name, archive.read(name))
                    for name in archive.namelist()
                    if name.lower().endswith(".xml")
                )
        except BadZipFile as error:
            raise SchemaMismatchError("GIRBO download is an invalid ZIP") from error
        if not members:
            raise SchemaMismatchError("GIRBO ZIP contains no XML report")
        return members
    return ((None, content),)


def _write_once(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise InvalidDataError("immutable GIRBO RAW member differs")
        return
    path.write_bytes(content)
    path.chmod(0o444)


def _document_identity(document: dict[str, Any]) -> str:
    return ":".join(
        str(document.get(name) or "")
        for name in (
            "id",
            "inn",
            "period",
            "periodType",
            "uploadDate",
            "fileType",
            "reportType",
        )
    )


def _manifest_name(document: dict[str, Any]) -> str:
    api_id = str(document.get("id") or "document").strip()
    safe_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in api_id
    ).strip("-")
    return f"manifest-{safe_id or 'document'}.json"


def girbo_worker_handler(
    context: HandlerContext, *, provider: GirboApiProvider | None = None
) -> HandlerResult:
    require_access()
    root = Path(str(context.schedule_metadata.get("raw_root") or "")).resolve()
    if not root.is_absolute():
        raise InvalidDataError("raw_root must be absolute")
    target_inns = tuple(
        str(item) for item in context.schedule_metadata.get("target_inns") or ()
    )
    years = tuple(
        int(item) for item in context.schedule_metadata.get("years") or ()
    )
    if not target_inns or not years:
        raise InvalidDataError("GIRBO target cohort/years are empty")
    root = root / SOURCE_ID
    root.mkdir(parents=True, exist_ok=True)
    active_provider = provider or GirboApiProvider()
    documents: list[tuple[str, dict[str, Any]]] = []
    for inn in target_inns:
        for year in years:
            context.ensure_active(now=utc_now())
            documents.extend(
                (inn, item)
                for item in active_provider.list_files(inn=inn, year=year)
            )
            context.heartbeat()
    release_identity = sha256(
        "\n".join(
            sorted(_document_identity(item) for _inn, item in documents)
        ).encode("utf-8")
    ).hexdigest()
    if release_identity == context.schedule_metadata.get(
        "accepted_release_identity"
    ):
        return HandlerResult(
            checksum_metadata={
                "check_only": True,
                "release_identity": release_identity,
            },
            counters=ExecutionCounters(records_seen=len(documents)),
        )

    normalized_lines: list[bytes] = []
    raw_refs: list[RawArtifactReference] = []
    newest: date | None = None
    for inn, document in documents:
        content, headers = active_provider.download_file(str(document["token"]))
        checksum = sha256(content).hexdigest()
        artifact_dir = root / checksum
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / "artifact.bin"
        _write_once(artifact, content)
        for member_name, xml_content in _xml_members(content):
            record = parse_girbo_xml(
                xml_content,
                requested_inn=inn,
                metadata=document,
                raw_checksum=checksum,
                member_name=member_name,
            )
            newest = max(
                newest or date.min,
                date.fromisoformat(record["source_data_date"]),
            )
            normalized_lines.append(
                (
                    json.dumps(record, ensure_ascii=False, sort_keys=True)
                    + "\n"
                ).encode()
            )
        stable_manifest = {
            "source_id": SOURCE_ID,
            "official_host": "api-bo.nalog.gov.ru",
            "inn": inn,
            "api_document_id": str(document.get("id") or ""),
            "file_name": str(document.get("fileName") or ""),
            "file_type": str(document.get("fileType") or ""),
            "report_type": str(document.get("reportType") or ""),
            "period": str(document.get("period") or ""),
            "period_type": str(document.get("periodType") or ""),
            "upload_date": str(document.get("uploadDate") or ""),
            "sha256": checksum,
            "size": len(content),
            "content_type": headers.get("content-type"),
            "immutable": True,
        }
        _write_once(
            artifact_dir / _manifest_name(document),
            (
                json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True)
                + "\n"
            ).encode(),
        )
        raw_refs.append(
            RawArtifactReference(
                artifact.as_uri(),
                checksum,
                {**stable_manifest, "retrieved_at": utc_now().isoformat()},
            )
        )
        context.heartbeat()
    normalized_content = b"".join(sorted(normalized_lines))
    normalized_checksum = sha256(normalized_content).hexdigest()
    normalized_path = root / normalized_checksum / "normalized.jsonl"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    _write_once(normalized_path, normalized_content)
    count = len(normalized_lines)
    counters = ExecutionCounters(
        records_seen=len(documents), records_written=count
    )
    context.report_counters(counters)
    return HandlerResult(
        raw_artifacts=tuple(raw_refs),
        staging_result=StagingResult(
            normalized_path.as_uri(),
            ValidationResult(
                accepted=True,
                metadata={
                    "release_identity": release_identity,
                    "source_data_date": (
                        newest
                        or date.fromisoformat(
                            context.schedule_metadata["checked_date"]
                        )
                    ).isoformat(),
                    "source_records": len(documents),
                    "normalized_reports": count,
                    "target_companies": len(target_inns),
                    "years": list(years),
                },
            ),
            checksum=normalized_checksum,
        ),
        checksum_metadata={"normalized_sha256": normalized_checksum},
        counters=counters,
    )


def _iter_jsonl(uri: str) -> Iterator[dict[str, Any]]:
    path = Path(urlparse(uri).path)
    if not path.is_file():
        raise InvalidDataError("GIRBO normalized snapshot is unavailable")
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def publish_girbo_result(
    session: Session, claim: Any, result: HandlerResult
) -> HandlerResult:
    dataset = session.scalar(
        select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update()
    )
    if dataset is None:
        raise InvalidDataError("GIRBO dataset is not registered")
    now = utc_now()
    if result.staging_result is None:
        if dataset.last_success_at is None:
            raise InvalidDataError(
                "GIRBO check-only run has no accepted publication"
            )
        source_records = int(
            (dataset.coverage or {}).get("source_records") or 0
        )
        summary = SourceChangeSummary(
            matched_companies=int(
                (dataset.coverage or {}).get("matched_companies") or 0
            ),
            new_facts=0,
            changed_facts=0,
            removed_or_expired_facts=0,
            unchanged_facts=int(dataset.record_count or 0),
            replayed_facts=0,
            quarantined_records=0,
            source_records=source_records,
            source_data_date=dataset.last_data_date,
            previous_source_data_date=dataset.last_data_date,
        )
        dataset.checked_at = now
        dataset.official_actual_until = now.date()
        dataset.operational_status = OperationalStatus.CURRENT
        dataset.next_expected_update_at = now + CHECK_INTERVAL
        dataset.last_error = None
        dataset.last_error_at = None
        return replace(result, change_summary=summary)

    previous_date = dataset.last_data_date
    inserted = unchanged = corrected = 0
    matched_companies: set[int] = set()
    newest = date.min
    for row in _iter_jsonl(result.staging_result.staging_pointer):
        company = session.scalar(
            select(Company).where(Company.inn == row["inn"])
        )
        if company is None:
            continue
        matched_companies.add(company.id)
        newest = max(newest, date.fromisoformat(row["source_data_date"]))
        same = session.scalar(
            select(GirboAccountingReport).where(
                GirboAccountingReport.dataset_id == dataset.id,
                GirboAccountingReport.company_id == company.id,
                GirboAccountingReport.report_id == row["report_id"],
                GirboAccountingReport.raw_checksum == row["raw_checksum"],
            )
        )
        if same is not None:
            unchanged += 1
            continue
        max_revision = int(
            session.scalar(
                select(func.max(GirboAccountingReport.revision)).where(
                    GirboAccountingReport.dataset_id == dataset.id,
                    GirboAccountingReport.company_id == company.id,
                    GirboAccountingReport.report_id == row["report_id"],
                )
            )
            or 0
        )
        session.execute(
            update(GirboAccountingReport)
            .where(
                GirboAccountingReport.dataset_id == dataset.id,
                GirboAccountingReport.company_id == company.id,
                GirboAccountingReport.report_id == row["report_id"],
                GirboAccountingReport.reporting_year == row["reporting_year"],
            )
            .values(is_current=False)
        )
        correction_date = (
            date.fromisoformat(row["correction_date"])
            if row.get("correction_date")
            else (
                date.fromisoformat(row["source_data_date"])
                if max_revision
                else None
            )
        )
        session.add(
            GirboAccountingReport(
                dataset_id=dataset.id,
                company_id=company.id,
                report_id=row["report_id"],
                revision=max_revision + 1,
                reporting_year=row["reporting_year"],
                publication_date=date.fromisoformat(row["publication_date"]),
                correction_date=correction_date,
                source_data_date=date.fromisoformat(row["source_data_date"]),
                is_current=True,
                revenue=row.get("revenue"),
                expenses=row.get("expenses"),
                profit_loss=row.get("profit_loss"),
                assets=row.get("assets"),
                liabilities=row.get("liabilities"),
                equity=row.get("equity"),
                statement_values=row["statement_values"],
                raw_checksum=row["raw_checksum"],
                retrieved_at=now,
            )
        )
        inserted += 1
        corrected += int(max_revision > 0)
    validation = result.staging_result.validation.metadata
    source_records = int(validation["source_records"])
    source_date = (
        newest
        if newest != date.min
        else date.fromisoformat(validation["source_data_date"])
    )
    summary = SourceChangeSummary(
        matched_companies=len(matched_companies),
        new_facts=inserted - corrected,
        changed_facts=corrected,
        removed_or_expired_facts=0,
        unchanged_facts=unchanged,
        replayed_facts=0,
        quarantined_records=0,
        source_records=source_records,
        source_data_date=source_date,
        previous_source_data_date=previous_date,
        unavailable_reasons={
            "previous_source_data_date": "first successful publication"
        }
        if previous_date is None
        else {},
    )
    dataset.enabled = True
    dataset.dataset_kind = "on_demand_api"
    dataset.freshness_policy = "daily"
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.operational_status = OperationalStatus.CURRENT
    dataset.last_success_at = dataset.checked_at = dataset.published_at = now
    dataset.last_data_date = source_date
    dataset.source_as_of = datetime.combine(
        source_date, datetime.min.time(), tzinfo=timezone.utc
    )
    dataset.official_actual_until = now.date()
    dataset.record_count = int(
        session.scalar(
            select(func.count())
            .select_from(GirboAccountingReport)
            .where(
                GirboAccountingReport.dataset_id == dataset.id,
                GirboAccountingReport.is_current.is_(True),
            )
        )
        or 0
    )
    dataset.coverage = {
        "source_records": source_records,
        "normalized_reports": int(validation["normalized_reports"]),
        "matched_companies": len(matched_companies),
        "published_facts": inserted,
        "corrected_facts": corrected,
        "years": list(validation["years"]),
        "historical_corrections_preserved": True,
        "api_projection": "girbo_accounting_reports",
        "card_projection": "company_card.accounting_reports",
        "change_summary": summary.as_dict(),
    }
    dataset.next_expected_update_at = now + CHECK_INTERVAL
    return replace(
        result,
        change_summary=summary,
        counters=ExecutionCounters(
            records_seen=source_records,
            records_written=inserted,
            records_published=inserted,
        ),
    )


def register_girbo_worker(session: Session, registry: HandlerRegistry) -> Any:
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=HANDLER_VERSION,
        handler=girbo_worker_handler,
        publisher=publish_girbo_result,
        approved=True,
        live=False,
        fixture=False,
        metadata={
            "mode": "official_oauth2_rest_api",
            "official_host": "api-bo.nalog.gov.ru",
            "api_instruction": "https://bo.nalog.gov.ru/REST_API.zip",
            "credentials_in_job_metadata": False,
        },
    )


def _selected_years(
    provider: GirboApiProvider, environ: dict[str, str]
) -> tuple[int, ...]:
    available = provider.available_years()
    configured = str(environ.get("GIRBO_YEARS") or "").strip()
    if not configured:
        return available[-5:]
    try:
        requested = tuple(
            sorted(
                {
                    int(value.strip())
                    for value in configured.split(",")
                    if value.strip()
                }
            )
        )
    except ValueError as error:
        raise InvalidDataError(
            "GIRBO_YEARS must contain comma-separated years"
        ) from error
    unknown = set(requested) - set(available)
    if unknown:
        raise InvalidDataError(
            "GIRBO_YEARS are unavailable from official API: "
            + ", ".join(str(value) for value in sorted(unknown))
        )
    return requested


def schedule_girbo_check(
    session: Session,
    *,
    raw_root: Path,
    now: datetime | None = None,
    cohort_limit: int = 500,
    provider: GirboApiProvider | None = None,
) -> JobCreation:
    require_access()
    now = now or utc_now()
    approval = session.get(
        WorkerHandlerRegistration, (SOURCE_ID, HANDLER_VERSION)
    )
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
    ):
        raise HandlerNotRegisteredError(
            f"durable handler approval is missing: {SOURCE_ID}@{HANDLER_VERSION}"
        )
    target_inns = tuple(
        session.scalars(
            select(Company.inn)
            .where(Company.entity_type == "legal")
            .order_by(Company.id)
            .limit(cohort_limit)
        )
    )
    if not target_inns:
        raise InvalidDataError("GIRBO Master legal-entity cohort is empty")
    active_provider = provider or GirboApiProvider()
    years = _selected_years(active_provider, active_provider.environ)
    state = session.get(WorkerPublicationState, SOURCE_ID)
    accepted_identity = (
        ((state.validation_metadata or {}).get("validation") or {}).get(
            "release_identity"
        )
        if state is not None
        else None
    )
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="girbo_accounting_check",
        handler_version=HANDLER_VERSION,
        idempotency_key=(
            f"{SOURCE_ID}:check:{now.date().isoformat()}:{HANDLER_VERSION}"
        ),
        schedule_metadata={
            "raw_root": str(Path(raw_root).resolve()),
            "target_inns": list(target_inns),
            "years": list(years),
            "accepted_release_identity": accepted_identity,
            "checked_date": now.date().isoformat(),
            "check_frequency": "daily",
            "publication_frequency": "official_api_corrections",
        },
        max_attempts=3,
        timeout_seconds=6 * 60 * 60,
        now=now,
    )


def girbo_card_projection(
    session: Session, *, company_id: int
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(GirboAccountingReport)
        .where(
            GirboAccountingReport.company_id == company_id,
            GirboAccountingReport.is_current.is_(True),
        )
        .order_by(GirboAccountingReport.reporting_year.desc())
    ).all()
    return [
        {
            "report_id": row.report_id,
            "reporting_year": row.reporting_year,
            "publication_date": (
                row.publication_date.isoformat()
                if row.publication_date
                else None
            ),
            "correction_date": (
                row.correction_date.isoformat() if row.correction_date else None
            ),
            "revenue": str(row.revenue) if row.revenue is not None else None,
            "expenses": str(row.expenses) if row.expenses is not None else None,
            "profit_loss": (
                str(row.profit_loss) if row.profit_loss is not None else None
            ),
            "assets": str(row.assets) if row.assets is not None else None,
            "liabilities": (
                str(row.liabilities) if row.liabilities is not None else None
            ),
            "equity": str(row.equity) if row.equity is not None else None,
            "source": "GIRBO",
            "source_data_date": row.source_data_date.isoformat(),
            "unit_code": str(
                ((row.statement_values or {}).get("_document") or {}).get(
                    "unit_code"
                )
                or ""
            ),
        }
        for row in rows
    ]
