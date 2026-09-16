from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select

from app.database.postgres import get_session
from app.ingestion.fns_sme_support import (
    cleanup_old_snapshots,
    create_ingestion_run,
    get_dataset_id,
    import_fns_sme_support_archive,
    mark_run_failed,
    publish_ingestion_run,
)
from app.models.source import IngestionRun
from app.providers.fns_sme_support_provider import (
    FnsSmeSupportProvider,
    FnsSmeSupportRelease,
)
from app.services.fns_sme_support_registry_service import (
    ensure_fns_sme_support_dataset,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data" / "fns" / "sme-support"
DEFAULT_BATCH_SIZE = 2000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_success(dataset_id: int, *, data_date, source_url: str | None):
    session = get_session()
    try:
        return session.execute(
            select(IngestionRun).where(
                IngestionRun.dataset_id == dataset_id,
                IngestionRun.status == "success",
                IngestionRun.data_date == data_date,
                IngestionRun.source_url == source_url,
            ).order_by(IngestionRun.id.desc()).limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()


def sync_fns_sme_support(
    *,
    provider: FnsSmeSupportProvider | None = None,
    archive_path: Path | None = None,
    release: FnsSmeSupportRelease | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cleanup_old: bool = True,
    force: bool = False,
) -> dict:
    ensure_fns_sme_support_dataset()
    dataset_id = get_dataset_id()
    provider = provider or FnsSmeSupportProvider()

    if archive_path is None:
        release = release or provider.discover_release()
        existing = _existing_success(
            dataset_id, data_date=release.data_date, source_url=release.data_url
        )
        if existing is not None and not force:
            return {
                "status": "already_current",
                "dataset_id": dataset_id,
                "run_id": existing.id,
                "data_date": existing.data_date,
                "source_url": existing.source_url,
                "sha256": existing.file_checksum,
                "details": existing.details or {},
            }
        filename = Path(urlparse(release.data_url).path).name
        archive_path = DEFAULT_DIR / filename
        run_id = create_ingestion_run(
            dataset_id=dataset_id,
            data_date=release.data_date,
            source_url=release.data_url,
            source_file_name=filename,
            checksum=None,
        )
        try:
            download = provider.download(release, archive_path)
            checksum = download["sha256"]
            with get_session() as session:
                run = session.get(IngestionRun, run_id)
                run.file_checksum = checksum
                run.details = {
                    **(run.details or {}),
                    "http_status": download["http_status"],
                    "download_bytes": download["bytes"],
                    "metadata_url": (
                        "https://www.nalog.gov.ru/opendata/7707329152-rsmppp/"
                    ),
                    "structure_url": release.structure_url,
                    "modified_date": str(release.modified_date) if release.modified_date else None,
                }
                session.commit()
        except Exception as error:
            mark_run_failed(run_id, error)
            raise
    else:
        archive_path = Path(archive_path)
        if release is None:
            raise ValueError("Для локального archive_path требуется release с data_date/source_url")
        checksum = _sha256(archive_path)
        run_id = create_ingestion_run(
            dataset_id=dataset_id,
            data_date=release.data_date,
            source_url=release.data_url,
            source_file_name=archive_path.name,
            checksum=checksum,
        )

    try:
        stats = import_fns_sme_support_archive(
            archive_path,
            dataset_id=dataset_id,
            run_id=run_id,
            data_date=release.data_date,
            batch_size=batch_size,
        )
        result = publish_ingestion_run(run_id, stats)
        result.update(
            {
                "status": "success",
                "source_url": release.data_url,
                "structure_url": release.structure_url,
                "sha256": checksum,
                "archive_path": str(archive_path),
            }
        )
        if cleanup_old:
            try:
                result["old_rows_deleted"] = cleanup_old_snapshots(
                    dataset_id=dataset_id, keep_run_id=run_id
                )
                result["cleanup_status"] = "success"
            except Exception as cleanup_error:
                # The new snapshot is already atomically published. Cleanup is
                # maintenance only and must never invalidate good source data.
                result["old_rows_deleted"] = 0
                result["cleanup_status"] = "failed"
                result["cleanup_error_type"] = type(cleanup_error).__name__
        else:
            result["old_rows_deleted"] = 0
            result["cleanup_status"] = "skipped"
        return result
    except Exception as error:
        mark_run_failed(run_id, error)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync official FNS SME-support bulk snapshot")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-old", action="store_true")
    args = parser.parse_args()
    result = sync_fns_sme_support(
        batch_size=args.batch_size,
        cleanup_old=not args.keep_old,
        force=args.force,
    )
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
