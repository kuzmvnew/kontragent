"""Official EGRUL/EGRIP full+delta adapters for Worker Foundation.

The FNS transport is credentialed FTPS.  Credentials are read only at the
transport boundary and never enter job metadata, logs, manifests or errors.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from ftplib import FTP_TLS
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

from lxml import etree
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.company import Company, CompanyManager
from app.models.registry_master import CompanyRegistryChange, MasterReplaySignal, RegistrySourceCheckpoint
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.worker.contracts import ExecutionCounters, HandlerContext, HandlerResult, RawArtifactReference, SourceChangeSummary, StagingResult, ValidationResult
from app.worker.errors import AccessRequiredError, HandlerNotRegisteredError, InvalidDataError, SchemaMismatchError, WorkerNetworkError
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


HANDLER_VERSION = "fns-registry-master-v1"
CHECK_INTERVAL = timedelta(days=1)
NORMALIZED_BATCH_SIZE = 1000
REQUIRED_ACCESS = (
    "FNS_REGISTRY_FTP_URL",
    "FNS_REGISTRY_USERNAME",
    "FNS_REGISTRY_PASSWORD",
    "FNS_REGISTRY_CLIENT_CERT",
    "FNS_REGISTRY_CLIENT_KEY",
)


@dataclass(frozen=True)
class RegistrySpec:
    source_id: str
    entity_type: str
    current_format: str
    compatible_formats: tuple[str, ...]
    directory_names: tuple[str, ...]
    record_tag: str


SPECS = {
    "fns_egrul": RegistrySpec(
        source_id="fns_egrul", entity_type="legal", current_format="4.08",
        compatible_formats=("4.08", "4.07"), directory_names=("EGRUL_408", "EGRUL_407"),
        record_tag="СвЮЛ",
    ),
    "fns_egrip": RegistrySpec(
        source_id="fns_egrip", entity_type="individual_entrepreneur", current_format="4.07",
        compatible_formats=("4.07", "4.06"), directory_names=("EGRIP_407", "EGRIP_406"),
        record_tag="СвИП",
    ),
}


@dataclass(frozen=True)
class RegistryArtifact:
    remote_path: str
    name: str
    data_date: date
    kind: str
    format_version: str
    size: int | None = None

    @property
    def identity(self) -> str:
        return f"{self.remote_path}:{self.size or 0}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "remote_path": self.remote_path,
            "name": self.name,
            "data_date": self.data_date.isoformat(),
            "kind": self.kind,
            "format_version": self.format_version,
            "size": self.size,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def access_state(environ: dict[str, str] | None = None) -> tuple[bool, tuple[str, ...]]:
    values = environ if environ is not None else os.environ
    missing = tuple(name for name in REQUIRED_ACCESS if not str(values.get(name) or "").strip())
    if not missing:
        for name in ("FNS_REGISTRY_CLIENT_CERT", "FNS_REGISTRY_CLIENT_KEY"):
            if not Path(values[name]).is_file():
                missing += (name,)
    return (not missing, missing)


def require_access(environ: dict[str, str] | None = None) -> None:
    ready, missing = access_state(environ)
    if not ready:
        raise AccessRequiredError(
            "FNS registry FTPS access is not configured; missing: " + ", ".join(sorted(set(missing)))
        )


def _parse_artifact(name: str, *, remote_path: str, size: int | None, format_version: str) -> RegistryArtifact | None:
    if not name.lower().endswith(".zip"):
        return None
    dates = re.findall(r"(?<!\d)(20\d{2})[._-]?(\d{2})[._-]?(\d{2})(?!\d)", name)
    if not dates:
        return None
    year, month, day = dates[-1]
    try:
        data_date = date(int(year), int(month), int(day))
    except ValueError:
        return None
    lowered = name.lower()
    kind = "full" if any(token in lowered for token in ("full", "fullset", "полный")) else "delta"
    return RegistryArtifact(remote_path, name, data_date, kind, format_version, size)


class FnsRegistryFtpsProvider:
    def __init__(self, environ: dict[str, str] | None = None):
        self.environ = environ if environ is not None else os.environ

    def _connect(self) -> FTP_TLS:
        require_access(self.environ)
        parsed = urlparse(self.environ["FNS_REGISTRY_FTP_URL"])
        if parsed.scheme not in {"ftp", "ftps"} or not parsed.hostname:
            raise InvalidDataError("FNS_REGISTRY_FTP_URL must be an ftp/ftps URL")
        context = ssl.create_default_context()
        context.load_cert_chain(
            self.environ["FNS_REGISTRY_CLIENT_CERT"], self.environ["FNS_REGISTRY_CLIENT_KEY"]
        )
        client = FTP_TLS(context=context, timeout=60)
        try:
            client.connect(parsed.hostname, parsed.port or 21)
            client.auth()
            client.login(self.environ["FNS_REGISTRY_USERNAME"], self.environ["FNS_REGISTRY_PASSWORD"])
            client.prot_p()
            if parsed.path and parsed.path != "/":
                client.cwd(parsed.path)
        except Exception as error:
            try:
                client.close()
            finally:
                raise WorkerNetworkError("FNS registry FTPS connection failed") from error
        return client

    def discover(self, spec: RegistrySpec) -> tuple[RegistryArtifact, ...]:
        client = self._connect()
        discovered: list[RegistryArtifact] = []
        try:
            for directory, format_version in zip(spec.directory_names, spec.compatible_formats):
                try:
                    rows = list(client.mlsd(directory, facts=("type", "size")))
                except Exception:
                    continue
                for name, facts in rows:
                    if facts.get("type") != "file":
                        continue
                    item = _parse_artifact(
                        name, remote_path=f"{directory}/{name}",
                        size=int(facts["size"]) if facts.get("size") else None,
                        format_version=format_version,
                    )
                    if item is not None:
                        discovered.append(item)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
        return tuple(sorted(discovered, key=lambda item: (item.data_date, item.name)))

    def download(self, remote_path: str, target: Path) -> None:
        client = self._connect()
        try:
            with target.open("wb") as stream:
                client.retrbinary(f"RETR {remote_path}", stream.write, blocksize=1024 * 1024)
        except Exception as error:
            target.unlink(missing_ok=True)
            raise WorkerNetworkError("FNS registry artifact download failed") from error
        finally:
            try:
                client.quit()
            except Exception:
                client.close()


def select_release_chain(
    artifacts: Iterable[RegistryArtifact], checkpoint: RegistrySourceCheckpoint | None
) -> tuple[RegistryArtifact, ...]:
    def assert_contiguous(selected: tuple[RegistryArtifact, ...], start: date) -> None:
        if not selected:
            return
        available_dates = sorted({item.data_date for item in selected})
        expected = start + timedelta(days=1)
        for actual in available_dates:
            if actual != expected:
                raise SchemaMismatchError(
                    f"FNS registry delta chain has a gap: expected {expected}, got {actual}"
                )
            expected += timedelta(days=1)

    items = tuple(artifacts)
    if not items:
        raise SchemaMismatchError("FNS registry directory contains no supported ZIP artifacts")
    if checkpoint is None or not checkpoint.baseline_accepted:
        full = [item for item in items if item.kind == "full"]
        if not full:
            raise SchemaMismatchError("FNS registry full baseline is unavailable")
        baseline = max(full, key=lambda item: (item.data_date, item.name))
        deltas = [item for item in items if item.kind == "delta" and item.data_date > baseline.data_date]
        ordered = tuple(sorted(deltas, key=lambda item: (item.data_date, item.name)))
        assert_contiguous(ordered, baseline.data_date)
        return (baseline, *ordered)
    cutoff = checkpoint.last_delta_date or checkpoint.last_full_date
    selected = tuple(
        sorted(
            (item for item in items if item.kind == "delta" and (cutoff is None or item.data_date > cutoff)),
            key=lambda item: (item.data_date, item.name),
        )
    )
    if cutoff is not None:
        assert_contiguous(selected, cutoff)
    return selected


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first(root: etree._Element, *names: str) -> etree._Element | None:
    wanted = set(names)
    return next((node for node in root.iter() if _local_name(node.tag) in wanted), None)


def _attr(node: etree._Element | None, *names: str) -> str | None:
    if node is None:
        return None
    for name in names:
        value = node.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    return None


def _record_from_element(element: etree._Element, spec: RegistrySpec) -> dict[str, Any] | None:
    inn = _attr(element, "ИНН", "ИННЮЛ", "ИННФЛ")
    ogrn = _attr(element, "ОГРН", "ОГРНИП")
    if not inn or (len(inn) not in (10, 12)) or not inn.isdigit():
        return None
    person = _first(element, "СвФЛ")
    name_node = _first(element, "СвНаимЮЛ", "СвФЛ")
    if spec.entity_type == "legal":
        full_name = _attr(name_node, "НаимЮЛПолн", "НаимЮЛ")
        short_name = _attr(name_node, "НаимСокр", "НаимЮЛСокр")
        display_name = full_name or short_name
    else:
        parts = [_attr(person, "Фамилия"), _attr(person, "Имя"), _attr(person, "Отчество")]
        display_name = " ".join(part for part in parts if part) or None
        full_name = display_name
        short_name = None
    if not display_name:
        return None
    address_node = _first(element, "АдресРФ", "СвАдресЮЛ", "СвАдрЮЛФИАС")
    address = _attr(address_node, "АдресРФ", "АдрРФ", "НаимРегион", "АдрТекст")
    okved_node = _first(element, "СвОКВЭДОсн")
    status_node = _first(element, "СвСтатус", "СвСтатусЮЛ", "СвСтатусИП")
    termination = _first(element, "СвПрекрЮЛ", "СвПрекрИП")
    leaders: list[dict[str, str | None]] = []
    if spec.entity_type == "legal":
        for leader in element.iter():
            if _local_name(leader.tag) not in {"СведДолжнФЛ", "СвРукОрг"}:
                continue
            leader_person = _first(leader, "СвФЛ")
            parts = [_attr(leader_person, "Фамилия"), _attr(leader_person, "Имя"), _attr(leader_person, "Отчество")]
            full = " ".join(part for part in parts if part)
            if full:
                position = _attr(_first(leader, "СвДолжн"), "НаимДолжн")
                leaders.append({"full_name": full, "position": position})
    data_date = _date(_attr(element, "ДатаВып", "ДатаФорм"))
    record_key = f"{ogrn or inn}:{data_date.isoformat() if data_date else 'unknown'}"
    return {
        "inn": inn,
        "kpp": _attr(element, "КПП"),
        "ogrn": ogrn,
        "entity_type": spec.entity_type,
        "name": display_name,
        "short_name": short_name,
        "full_name": full_name,
        "status": _attr(status_node, "НаимСтатусЮЛ", "НаимСтатусИП", "КодСтатусЮЛ", "КодСтатусИП"),
        "registration_date": (_date(_attr(element, "ДатаОГРН", "ДатаОГРНИП")) or None).isoformat() if _date(_attr(element, "ДатаОГРН", "ДатаОГРНИП")) else None,
        "termination_date": (_date(_attr(termination, "ДатаПрекрЮЛ", "ДатаПрекрИП")) or None).isoformat() if _date(_attr(termination, "ДатаПрекрЮЛ", "ДатаПрекрИП")) else None,
        "address": address,
        "region_code": _attr(address_node, "КодРегион"),
        "okved": _attr(okved_node, "КодОКВЭД"),
        "activity": _attr(okved_node, "НаимОКВЭД"),
        "leaders": leaders,
        "source_data_date": data_date.isoformat() if data_date else None,
        "source_record_key": record_key,
    }


def iter_registry_xml(stream: Any, spec: RegistrySpec) -> Iterator[dict[str, Any]]:
    """Stream normalized records while clearing parsed XML subtrees."""

    try:
        context = etree.iterparse(stream, events=("end",), recover=False, huge_tree=True)
        for _event, element in context:
            if _local_name(element.tag) != spec.record_tag:
                continue
            record = _record_from_element(element, spec)
            if record is not None:
                yield record
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
    except etree.XMLSyntaxError as error:
        raise SchemaMismatchError("FNS registry XML is not well formed") from error


def normalize_archives(paths: Iterable[Path], *, spec: RegistrySpec, output: Path) -> tuple[int, int, str]:
    seen = rejected = 0
    digest = sha256()
    try:
        with output.open("xb") as target:
            for path in paths:
                try:
                    archive = ZipFile(path)
                except BadZipFile as error:
                    raise SchemaMismatchError("FNS registry artifact is not a ZIP") from error
                with archive:
                    names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
                    if not names:
                        raise SchemaMismatchError("FNS registry ZIP contains no XML")
                    for name in names:
                        with archive.open(name) as source:
                            for record in iter_registry_xml(source, spec):
                                seen += 1
                                line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                                target.write(line)
                                digest.update(line)
        return seen, rejected, digest.hexdigest()
    except Exception:
        output.unlink(missing_ok=True)
        raise


def _persist_download(
    provider: FnsRegistryFtpsProvider,
    artifact: RegistryArtifact,
    root: Path,
    *,
    source_id: str,
) -> tuple[Path, str, int]:
    temp_fd, temp_name = tempfile.mkstemp(prefix="fns-registry-", suffix=".zip", dir=root)
    os.close(temp_fd)
    temp = Path(temp_name)
    try:
        provider.download(artifact.remote_path, temp)
        digest_object = sha256()
        size = 0
        with temp.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest_object.update(chunk)
                size += len(chunk)
        digest = digest_object.hexdigest()
        directory = root / source_id / digest
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / artifact.name
        if target.exists():
            target_digest = sha256()
            with target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    target_digest.update(chunk)
            if target_digest.hexdigest() != digest:
                raise InvalidDataError("immutable FNS registry RAW artifact differs")
        else:
            os.link(temp, target)
            target.chmod(0o444)
        return target, digest, size
    finally:
        temp.unlink(missing_ok=True)


def run_registry_handler(context: HandlerContext, *, spec: RegistrySpec) -> HandlerResult:
    metadata = context.schedule_metadata
    artifacts = tuple(
        RegistryArtifact(
            remote_path=item["remote_path"], name=item["name"], data_date=date.fromisoformat(item["data_date"]),
            kind=item["kind"], format_version=item["format_version"], size=item.get("size"),
        ) for item in metadata.get("artifacts", [])
    )
    if not artifacts:
        # A no-change discovery is still a successful check and never downloads.
        return HandlerResult(
            checksum_metadata={"check_only": True, "release_identity": metadata["release_identity"]},
            counters=ExecutionCounters(),
        )
    root = Path(str(metadata["raw_root"])).resolve()
    if not root.is_absolute():
        raise InvalidDataError("raw_root must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    provider = FnsRegistryFtpsProvider()
    raw_refs: list[RawArtifactReference] = []
    paths: list[Path] = []
    for artifact in artifacts:
        context.ensure_active(now=utc_now())
        path, checksum, size = _persist_download(
            provider, artifact, root, source_id=spec.source_id
        )
        paths.append(path)
        manifest = {
            "source_id": spec.source_id, "official_remote_path": artifact.remote_path,
            "source_data_date": artifact.data_date.isoformat(), "kind": artifact.kind,
            "format_version": artifact.format_version, "sha256": checksum, "size": size,
            "retrieved_at": utc_now().isoformat(), "immutable": True,
        }
        manifest_path = path.parent / "manifest.json"
        payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
        if not manifest_path.exists():
            manifest_path.write_bytes(payload)
            manifest_path.chmod(0o444)
        elif manifest_path.read_bytes() != payload:
            # Retrieval time is observation metadata and must not rewrite the immutable manifest.
            manifest = json.loads(manifest_path.read_text())
        raw_refs.append(RawArtifactReference(path.as_uri(), checksum, manifest))
        context.heartbeat()
    staging_dir = paths[-1].parent
    staging = staging_dir / f"normalized-{context.run_id}.jsonl"
    records, rejected, normalized_checksum = normalize_archives(paths, spec=spec, output=staging)
    context.report_counters(ExecutionCounters(records_seen=records + rejected, records_written=records, records_rejected=rejected))
    source_date = max(item.data_date for item in artifacts)
    return HandlerResult(
        raw_artifacts=tuple(raw_refs),
        staging_result=StagingResult(
            staging_pointer=staging.as_uri(), checksum=normalized_checksum,
            validation=ValidationResult(accepted=True, metadata={
                "release_identity": metadata["release_identity"], "source_data_date": source_date.isoformat(),
                "source_records": records + rejected, "imported_records": records,
                "rejected_records": rejected, "chain": [item.as_dict() for item in artifacts],
            }),
        ),
        checksum_metadata={"normalized_sha256": normalized_checksum},
        counters=ExecutionCounters(records_seen=records + rejected, records_written=records, records_rejected=rejected),
    )


def _file_path(uri: str) -> Path:
    parsed = urlparse(uri)
    path = Path(parsed.path)
    if parsed.scheme != "file" or not path.is_file():
        raise InvalidDataError("FNS registry normalized snapshot is unavailable")
    return path


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


MASTER_FIELDS = ("entity_type", "name", "kpp", "ogrn", "short_name", "full_name", "status", "registration_date", "termination_date", "address", "region_code", "okved", "activity")


def _parsed_value(field: str, value: Any) -> Any:
    if field in {"registration_date", "termination_date"} and value:
        return date.fromisoformat(value)
    return value


def _change_types(old: dict[str, Any], new: dict[str, Any], *, created: bool) -> list[tuple[str, dict[str, Any]]]:
    def json_value(value: Any) -> Any:
        return value.isoformat() if isinstance(value, date) else value

    if created:
        return [("created", {field: json_value(new.get(field)) for field in MASTER_FIELDS if new.get(field) is not None})]
    groups = {
        "identity_changed": ("name", "short_name", "full_name", "kpp", "ogrn"),
        "status_changed": ("status",), "address_changed": ("address", "region_code"),
        "okved_changed": ("okved", "activity"), "terminated": ("termination_date",),
    }
    result = []
    for event, fields in groups.items():
        changed = {
            field: {"before": json_value(old.get(field)), "after": json_value(new.get(field))}
            for field in fields
            if old.get(field) != new.get(field) and new.get(field) is not None
        }
        if changed:
            result.append((event, changed))
    return result


def _emit_change_and_replays(
    session: Session,
    *,
    claim: Any,
    spec: RegistrySpec,
    company: Company,
    source_date: date,
    source_record_key: str,
    event_type: str,
    changed_fields: dict[str, Any],
    now: datetime,
) -> None:
    change = CompanyRegistryChange(
        source_id=spec.source_id, company_id=company.id, run_id=claim.run_id,
        inn=company.inn, event_type=event_type, changed_fields=changed_fields,
        source_data_date=source_date,
        source_record_key=f"{source_record_key}:{event_type}",
    )
    session.add(change)
    session.flush()
    target_ids = tuple(session.scalars(
        select(DataSet.code).where(
            DataSet.enabled.is_(True),
            DataSet.code.notin_((spec.source_id, "fns_tax_debt", "fns_egrul", "fns_egrip")),
        )
    ))
    for target_id in target_ids:
        session.execute(
            pg_insert(MasterReplaySignal)
            .values(
                company_id=company.id, target_source_id=target_id,
                registry_change_id=change.id, status="pending",
            )
            .on_conflict_do_nothing()
        )
        target = session.scalar(
            select(DataSet).where(DataSet.code == target_id).with_for_update()
        )
        if target is not None:
            target.next_expected_update_at = now


def publish_registry_result(session: Session, claim: Any, result: HandlerResult, *, spec: RegistrySpec) -> HandlerResult:
    dataset = session.scalar(select(DataSet).where(DataSet.code == spec.source_id).with_for_update())
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {spec.source_id}")
    state = session.scalar(select(WorkerPublicationState).where(WorkerPublicationState.source_id == spec.source_id).with_for_update())
    previous_identity = (((state.validation_metadata or {}).get("validation") or {}).get("release_identity") if state else None)
    release_identity = str(claim.schedule_metadata["release_identity"])
    now = utc_now()
    if result.staging_result is None or previous_identity == release_identity:
        dataset.checked_at = now
        dataset.official_actual_until = now.date()
        dataset.last_error = None
        dataset.last_error_at = None
        dataset.next_expected_update_at = now + CHECK_INTERVAL
        if dataset.last_success_at:
            dataset.operational_status = OperationalStatus.CURRENT
        coverage = dict(dataset.coverage or {})
        successful_checks = max(
            1, int(coverage.get("successful_scheduled_checks") or 0)
        ) + 1
        coverage.update(
            {
                "successful_scheduled_checks": successful_checks,
                "operational_accepted": successful_checks >= 2,
            }
        )
        dataset.coverage = coverage
        summary = SourceChangeSummary(
            matched_companies=0, new_facts=0, changed_facts=0, removed_or_expired_facts=0,
            unchanged_facts=int(dataset.record_count or 0), replayed_facts=0, quarantined_records=0,
            source_records=int(dataset.record_count or 0), source_data_date=dataset.last_data_date,
            previous_source_data_date=dataset.last_data_date,
            unavailable_reasons={"source_data_date": "no accepted release", "previous_source_data_date": "no accepted release"} if dataset.last_data_date is None else {},
        )
        return replace(result, change_summary=summary)

    validation = result.staging_result.validation.metadata
    source_date = date.fromisoformat(str(validation["source_data_date"]))
    checkpoint = session.get(RegistrySourceCheckpoint, spec.source_id)
    if checkpoint is None:
        checkpoint = RegistrySourceCheckpoint(source_id=spec.source_id, format_version=spec.current_format, baseline_accepted=False, cursor={})
        session.add(checkpoint)
    chain = list(validation["chain"])
    includes_full = any(item["kind"] == "full" for item in chain)
    if not checkpoint.baseline_accepted and not includes_full:
        raise InvalidDataError("FNS registry delta cannot publish before full baseline")
    limit = int(claim.schedule_metadata.get("entity_limit") or 0)
    if limit <= 0:
        raise InvalidDataError("controlled Master publication requires a positive entity limit")
    if includes_full and (
        not claim.schedule_metadata.get("baseline_approved")
        or not str(claim.schedule_metadata.get("backup_reference") or "").strip()
    ):
        raise InvalidDataError(
            "controlled full baseline requires an entity limit, owner approval and verified backup reference"
        )
    existing_entity_total = int(session.scalar(
        select(func.count()).select_from(Company).where(Company.entity_type == spec.entity_type)
    ) or 0)
    existing_inns = set(session.scalars(
        select(Company.inn).where(Company.entity_type == spec.entity_type)
    ))
    inserted = changed = unchanged = leaders_changed = 0
    matched = 0
    for row in _iter_jsonl(_file_path(result.staging_result.staging_pointer)):
        if (
            row["inn"] not in existing_inns
            and existing_entity_total + inserted >= limit
        ):
            continue
        company = session.scalar(select(Company).where(Company.inn == row["inn"]).with_for_update())
        created = company is None
        old = {field: getattr(company, field, None) for field in MASTER_FIELDS} if company else {}
        if company is None:
            company = Company(inn=row["inn"], name=row["name"], entity_type=spec.entity_type)
            session.add(company)
            session.flush()
            existing_inns.add(row["inn"])
            inserted += 1
        else:
            matched += 1
        row_source_date = (
            date.fromisoformat(row["source_data_date"])
            if row.get("source_data_date")
            else source_date
        )
        new = {field: _parsed_value(field, row.get(field)) for field in MASTER_FIELDS}
        events = _change_types(old, new, created=created)
        for field, value in new.items():
            if value is not None:
                setattr(company, field, value)
        company.master_dataset_id = dataset.id
        company.master_data_date = row_source_date
        company.source = "fns"
        company.source_updated_at = now
        if events:
            changed += int(not created)
        else:
            unchanged += 1
        for event_type, fields in events:
            _emit_change_and_replays(
                session, claim=claim, spec=spec, company=company,
                source_date=row_source_date, source_record_key=row["source_record_key"],
                event_type=event_type, changed_fields=fields, now=now,
            )
        if spec.entity_type == "legal":
            current = {manager.full_name: manager for manager in session.scalars(select(CompanyManager).where(CompanyManager.company_id == company.id, CompanyManager.is_current.is_(True)))}
            incoming_names = {leader["full_name"] for leader in row.get("leaders") or []}
            previous_leaders = {
                name: manager.position for name, manager in current.items()
                if manager.source_dataset_id == dataset.id
            }
            incoming_leaders = {
                leader["full_name"]: leader.get("position")
                for leader in row.get("leaders") or []
            }
            for name, manager in current.items():
                if name not in incoming_names:
                    manager.is_current = False
                    leaders_changed += 1
            for leader in row.get("leaders") or []:
                manager = current.get(leader["full_name"])
                if manager is None:
                    session.add(CompanyManager(
                        company_id=company.id, full_name=leader["full_name"], position=leader.get("position"),
                        is_current=True, source="fns", source_dataset_id=dataset.id,
                        source_data_date=row_source_date, source_record_key=row["source_record_key"], observed_at=now,
                    ))
                    leaders_changed += 1
                else:
                    manager.position = leader.get("position")
                    manager.source_dataset_id = dataset.id
                    manager.source_data_date = row_source_date
                    manager.source_record_key = row["source_record_key"]
                    manager.observed_at = now
            if previous_leaders != incoming_leaders:
                _emit_change_and_replays(
                    session, claim=claim, spec=spec, company=company,
                    source_date=row_source_date, source_record_key=row["source_record_key"],
                    event_type="leader_changed",
                    changed_fields={"before": previous_leaders, "after": incoming_leaders},
                    now=now,
                )
    checkpoint.baseline_accepted = checkpoint.baseline_accepted or includes_full
    checkpoint.last_full_date = max((date.fromisoformat(item["data_date"]) for item in chain if item["kind"] == "full"), default=checkpoint.last_full_date)
    checkpoint.last_delta_date = max((date.fromisoformat(item["data_date"]) for item in chain if item["kind"] == "delta"), default=checkpoint.last_delta_date)
    checkpoint.last_release_identity = release_identity
    checkpoint.format_version = str(chain[-1]["format_version"])
    checkpoint.cursor = {"artifacts": [item["remote_path"] for item in chain]}
    previous_date = dataset.last_data_date
    published = inserted + matched
    current_master_records = int(session.scalar(
        select(func.count()).select_from(Company).where(
            Company.master_dataset_id == dataset.id,
            Company.entity_type == spec.entity_type,
        )
    ) or 0)
    summary = SourceChangeSummary(
        matched_companies=matched, new_facts=inserted, changed_facts=changed + leaders_changed,
        removed_or_expired_facts=0, unchanged_facts=unchanged, replayed_facts=0,
        quarantined_records=int(validation["rejected_records"]), source_records=int(validation["source_records"]),
        source_data_date=source_date, previous_source_data_date=previous_date,
        unavailable_reasons={"previous_source_data_date": "first successful publication"} if previous_date is None else {},
    )
    dataset.enabled = True
    dataset.dataset_kind = "bulk_snapshot"
    dataset.freshness_policy = "daily"
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.operational_status = OperationalStatus.CURRENT
    dataset.last_success_at = dataset.checked_at = dataset.published_at = now
    dataset.last_data_date = source_date
    dataset.official_actual_until = now.date()
    dataset.source_as_of = datetime.combine(source_date, datetime.min.time(), tzinfo=timezone.utc)
    dataset.record_count = current_master_records
    successful_checks = int(
        (dataset.coverage or {}).get("successful_scheduled_checks") or 0
    ) + 1
    dataset.coverage = {
        "master_inserted": inserted, "master_matched": matched, "master_changed": changed,
        "master_current_records": current_master_records,
        "manager_changes": leaders_changed, "format_version": checkpoint.format_version,
        "release_identity": release_identity, "change_summary": summary.as_dict(),
        "successful_scheduled_checks": successful_checks,
        "operational_accepted": successful_checks >= 2,
    }
    dataset.next_expected_update_at = now + CHECK_INTERVAL
    return replace(result, change_summary=summary, counters=ExecutionCounters(
        records_seen=int(validation["source_records"]), records_written=published,
        records_rejected=int(validation["rejected_records"]), records_published=published,
    ))


def fns_egrul_handler(context: HandlerContext) -> HandlerResult:
    return run_registry_handler(context, spec=SPECS["fns_egrul"])


def fns_egrip_handler(context: HandlerContext) -> HandlerResult:
    return run_registry_handler(context, spec=SPECS["fns_egrip"])


def publish_egrul(session, claim, result):
    return publish_registry_result(session, claim, result, spec=SPECS["fns_egrul"])


def publish_egrip(session, claim, result):
    return publish_registry_result(session, claim, result, spec=SPECS["fns_egrip"])


def register_fns_registry_workers(session: Session, registry: HandlerRegistry) -> tuple[Any, Any]:
    registrations = []
    for source_id, handler, publisher in (
        ("fns_egrul", fns_egrul_handler, publish_egrul),
        ("fns_egrip", fns_egrip_handler, publish_egrip),
    ):
        registrations.append(register_handler(
            session, registry, source_id=source_id, version=HANDLER_VERSION,
            handler=handler, publisher=publisher, approved=True, live=False, fixture=False,
            metadata={"mode": "official_ftps_full_delta", "credentials_in_job_metadata": False},
        ))
    return tuple(registrations)


def schedule_fns_registry_check(session: Session, *, source_id: str, raw_root: Path, now: datetime | None = None, provider: FnsRegistryFtpsProvider | None = None) -> JobCreation:
    spec = SPECS[source_id]
    now = now or utc_now()
    approval = session.get(WorkerHandlerRegistration, (source_id, HANDLER_VERSION))
    if approval is None or not approval.approved or not approval.enabled or approval.live_mode:
        raise HandlerNotRegisteredError(f"durable handler approval is missing: {source_id}@{HANDLER_VERSION}")
    require_access()
    discovered = (provider or FnsRegistryFtpsProvider()).discover(spec)
    checkpoint = session.get(RegistrySourceCheckpoint, source_id)
    chain = select_release_chain(discovered, checkpoint)
    release_identity = sha256("\n".join(item.identity for item in chain).encode()).hexdigest() if chain else str(checkpoint.last_release_identity if checkpoint else "empty")
    includes_full = any(item.kind == "full" for item in chain)
    baseline_approved = os.environ.get("FNS_MASTER_BASELINE_APPROVED") == "1"
    backup_reference = str(os.environ.get("FNS_MASTER_BACKUP_REFERENCE") or "").strip()
    if includes_full and (not baseline_approved or not backup_reference):
        raise AccessRequiredError(
            "FNS Master full baseline requires FNS_MASTER_BASELINE_APPROVED=1 and FNS_MASTER_BACKUP_REFERENCE"
        )
    return create_job(
        session, source_id=source_id, job_type=f"{source_id}_check", handler_version=HANDLER_VERSION,
        idempotency_key=f"{source_id}:check:{now.date().isoformat()}:{release_identity}:{HANDLER_VERSION}",
        schedule_metadata={
            "raw_root": str(Path(raw_root).resolve()), "artifacts": [item.as_dict() for item in chain],
            "release_identity": release_identity,
            "entity_limit": 450 if spec.entity_type == "legal" else 50,
            "baseline_approved": baseline_approved,
            "backup_reference": backup_reference or None,
            "check_frequency": "daily", "publication_frequency": "full_plus_daily_delta",
        },
        max_attempts=3, timeout_seconds=6 * 60 * 60, now=now,
    )
