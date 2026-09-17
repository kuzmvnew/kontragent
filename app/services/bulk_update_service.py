"""Validated, locked and atomic orchestration for bulk dataset snapshots."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Generic, TypeVar
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select, update

from app.database.postgres import get_session
from app.models.source import DataSet, DatasetPublication
from app.services.data_readiness_service import (
    DatasetLockedError,
    acquire_dataset_lock,
    fail_update_run,
    finish_update_run,
    release_dataset_lock,
    start_update_run,
)


T = TypeVar("T")


class BulkUpdateError(RuntimeError):
    code = "bulk_update_failed"
    retryable = False
    source_blocked = False


class DownloadError(BulkUpdateError):
    code = "download_failed"
    retryable = True


class IntegrityError(BulkUpdateError):
    code = "integrity_failed"


class PartialFileError(IntegrityError):
    code = "partial_file"
    source_blocked = True


class ParseValidationError(BulkUpdateError):
    code = "parse_failed"


@dataclass(frozen=True)
class ArtifactIntegrity:
    checksum: str
    size_bytes: int
    file_count: int | None = None


@dataclass(frozen=True)
class ParsedSnapshot(Generic[T]):
    payload: T
    source_as_of: datetime | None
    record_count: int
    records_rejected: int = 0
    duplicates: int = 0
    conflicts: int = 0
    coverage: dict | None = None
    version: str | None = None


def validate_artifact(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    minimum_bytes: int = 1,
    expected_file_count: int | None = None,
) -> ArtifactIntegrity:
    artifact = Path(path)
    if not artifact.is_file():
        raise PartialFileError(f"Файл не найден: {artifact}")
    size = artifact.stat().st_size
    if size < minimum_bytes:
        raise PartialFileError(f"Неполный файл: {size} bytes")
    digest = sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum = digest.hexdigest()
    if expected_sha256 and checksum != expected_sha256.lower():
        raise IntegrityError("SHA-256 не совпадает")
    file_count = None
    if artifact.suffix.lower() == ".zip":
        try:
            with ZipFile(artifact) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise PartialFileError(f"ZIP CRC error: {bad_member}")
                file_count = len([item for item in archive.infolist() if not item.is_dir()])
        except BadZipFile as error:
            raise PartialFileError("ZIP EOF/central directory validation failed") from error
        if expected_file_count is not None and file_count != expected_file_count:
            raise IntegrityError(f"Ожидалось файлов: {expected_file_count}; получено: {file_count}")
    return ArtifactIntegrity(checksum=checksum, size_bytes=size, file_count=file_count)


class BulkUpdatePipeline(Generic[T]):
    """Callback-based pipeline; the publish callback shares one DB transaction."""

    def __init__(
        self,
        *,
        download: Callable[[], Path],
        parse: Callable[[Path], ParsedSnapshot[T]],
        publish: Callable[[object, T], None],
        expected_sha256: str | None = None,
        minimum_bytes: int = 1,
        expected_file_count: int | None = None,
    ):
        self.download = download
        self.parse = parse
        self.publish = publish
        self.expected_sha256 = expected_sha256
        self.minimum_bytes = minimum_bytes
        self.expected_file_count = expected_file_count

    def run(self, dataset_code: str, *, trigger: str = "scheduled", owner: str | None = None) -> int:
        owner = owner or f"bulk:{uuid4()}"
        if not acquire_dataset_lock(dataset_code, owner):
            raise DatasetLockedError(f"Dataset уже обновляется: {dataset_code}")
        run_id = start_update_run(dataset_code, trigger=trigger, lock_owner=owner)
        try:
            try:
                artifact = self.download()
            except BulkUpdateError:
                raise
            except Exception as error:
                raise DownloadError(str(error)) from error
            integrity = validate_artifact(
                artifact,
                expected_sha256=self.expected_sha256,
                minimum_bytes=self.minimum_bytes,
                expected_file_count=self.expected_file_count,
            )
            try:
                parsed = self.parse(artifact)
            except BulkUpdateError:
                raise
            except Exception as error:
                raise ParseValidationError(str(error)) from error
            if parsed.record_count < 0 or parsed.records_rejected < 0:
                raise ParseValidationError("Счётчики parser не могут быть отрицательными")
            version = parsed.version or integrity.checksum
            published_at = datetime.now(timezone.utc)
            session = get_session()
            try:
                dataset = session.scalar(select(DataSet).where(DataSet.code == dataset_code).with_for_update())
                if dataset is None:
                    raise ValueError(f"Dataset не найден: {dataset_code}")
                existing = session.scalar(select(DatasetPublication).where(
                    DatasetPublication.dataset_id == dataset.id,
                    DatasetPublication.version == version,
                    DatasetPublication.is_active.is_(True),
                ))
                if existing is not None:
                    session.rollback()
                    finish_update_run(
                        run_id,
                        source_as_of=existing.source_as_of,
                        retrieved_at=published_at,
                        published_at=existing.published_at,
                        record_count=existing.record_count or 0,
                        coverage=(existing.metadata_json or {}).get("coverage"),
                        checksum=existing.checksum,
                        version=existing.version,
                    )
                    return run_id
                publication = DatasetPublication(
                    dataset_id=dataset.id,
                    run_id=run_id,
                    version=version,
                    status="staging",
                    is_active=False,
                    source_as_of=parsed.source_as_of,
                    retrieved_at=published_at,
                    checksum=integrity.checksum,
                    record_count=parsed.record_count,
                    metadata_json={
                        "coverage": parsed.coverage,
                        "records_rejected": parsed.records_rejected,
                        "duplicates": parsed.duplicates,
                        "conflicts": parsed.conflicts,
                        "size_bytes": integrity.size_bytes,
                        "file_count": integrity.file_count,
                    },
                )
                session.add(publication)
                session.flush()
                # Domain rows and publication pointer commit together.
                self.publish(session, parsed.payload)
                session.execute(update(DatasetPublication).where(
                    DatasetPublication.dataset_id == dataset.id,
                    DatasetPublication.is_active.is_(True),
                ).values(is_active=False, status="superseded"))
                publication.is_active = True
                publication.status = "active"
                publication.published_at = published_at
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
            finish_update_run(
                run_id,
                source_as_of=parsed.source_as_of,
                retrieved_at=published_at,
                published_at=published_at,
                record_count=parsed.record_count,
                coverage=parsed.coverage,
                records_rejected=parsed.records_rejected,
                duplicates=parsed.duplicates,
                conflicts=parsed.conflicts,
                checksum=integrity.checksum,
                version=version,
            )
            return run_id
        except Exception as error:
            fail_update_run(
                run_id,
                error_code=getattr(error, "code", "publish_failed"),
                error_message=error,
                retryable=getattr(error, "retryable", False),
                source_blocked=getattr(error, "source_blocked", False),
            )
            raise
        finally:
            release_dataset_lock(dataset_code, owner)
