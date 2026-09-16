from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select

from app.database.postgres import get_session
from app.ingestion.fns_sme_support import (
    cleanup_old_snapshots, create_ingestion_run, get_dataset_id,
    import_fns_sme_support_archive, mark_run_failed, publish_ingestion_run,
)
from app.ingestion.fns_sme_support_integrity import POLICY
from app.models.source import IngestionRun
from app.providers.fns_sme_support_provider import FnsSmeSupportProvider, FnsSmeSupportRelease
from app.services.fns_sme_support_registry_service import ensure_fns_sme_support_dataset

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / 'data' / 'fns' / 'sme-support'
DEFAULT_BATCH_SIZE = 2000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_success(dataset_id, *, data_date, source_url):
    # The dated official artifact URL identifies the release. The XML ДатаСост
    # and metadata relevance date are deliberately not conflated.
    with get_session() as session:
        rows = session.execute(select(IngestionRun).where(
            IngestionRun.dataset_id == dataset_id, IngestionRun.status == 'success',
            IngestionRun.source_url == source_url,
        ).order_by(IngestionRun.id.desc()).limit(20)).scalars().all()
        for row in rows:
            details = row.details or {}
            integrity = details.get('integrity') or {}
            if details.get('complete_snapshot') and integrity.get('policy') == POLICY and integrity.get('parser_counts_match'):
                return row
    return None


def _verified_cached_download(path, dataset_id, release):
    if not path.is_file():
        return None
    with get_session() as session:
        previous = session.execute(select(IngestionRun).where(
            IngestionRun.dataset_id == dataset_id,
            IngestionRun.source_url == release.data_url,
            IngestionRun.file_checksum.is_not(None),
        ).order_by(IngestionRun.id.desc()).limit(20)).scalars().all()
        candidates = [(row.id, row.file_checksum, dict(row.details or {})) for row in previous]
    candidates = [item for item in candidates if item[2].get('http_status') == 200
                  and item[2].get('download_bytes') == path.stat().st_size]
    if not candidates:
        return None
    print('W1-003: проверяю SHA-256 уже скачанного архива', flush=True)
    checksum = _sha256(path)
    for old_id, old_hash, details in candidates:
        if checksum == old_hash:
            print('W1-003: архив подтверждён, повторное скачивание не требуется', flush=True)
            return {**details, 'sha256': checksum, 'bytes': path.stat().st_size,
                    'http_status': 200, 'reused_from_run_id': old_id, 'download_reused': True}
    return None


def sync_fns_sme_support(*, provider=None, archive_path=None, release=None,
                         batch_size=DEFAULT_BATCH_SIZE, cleanup_old=True, force=False):
    ensure_fns_sme_support_dataset()
    dataset_id = get_dataset_id()
    provider = provider or FnsSmeSupportProvider()
    if archive_path is None:
        print('W1-003: проверка официальной страницы ФНС', flush=True)
        release = release or provider.discover_release()
        existing = _existing_success(dataset_id, data_date=release.data_date, source_url=release.data_url)
        if existing is not None and not force:
            return {'status': 'already_current', 'dataset_id': dataset_id, 'run_id': existing.id,
                    'data_date': existing.data_date, 'source_url': existing.source_url,
                    'sha256': existing.file_checksum, 'details': existing.details or {}}
        filename = Path(urlparse(release.data_url).path).name
        archive_path = DEFAULT_DIR / filename
        download = None if force else _verified_cached_download(archive_path, dataset_id, release)
        run_id = create_ingestion_run(dataset_id=dataset_id, data_date=release.data_date,
            source_url=release.data_url, source_file_name=filename, checksum=None)
        try:
            if download is None:
                print('W1-003: скачивание официального ZIP', flush=True)
                download = provider.download(release, archive_path)
            checksum = download['sha256']
            with get_session() as session:
                run = session.get(IngestionRun, run_id)
                run.file_checksum = checksum
                run.details = {**(run.details or {}),
                    'http_status': download['http_status'], 'download_bytes': download['bytes'],
                    'download_reused': download.get('download_reused', False),
                    'reused_from_run_id': download.get('reused_from_run_id'),
                    'metadata_checked_at': datetime.now(timezone.utc).isoformat(),
                    'metadata_url': 'https://www.nalog.gov.ru/opendata/7707329152-rsmppp/',
                    'structure_url': release.structure_url,
                    'modified_date': str(release.modified_date) if release.modified_date else None}
                session.commit()
        except Exception as error:
            mark_run_failed(run_id, error)
            raise
    else:
        archive_path = Path(archive_path)
        if release is None:
            raise ValueError('Для локального archive_path требуется release с data_date/source_url')
        checksum = _sha256(archive_path)
        run_id = create_ingestion_run(dataset_id=dataset_id, data_date=release.data_date,
            source_url=release.data_url, source_file_name=archive_path.name, checksum=checksum)
    try:
        print('W1-003: проверка архива, затем загрузка в неопубликованный snapshot', flush=True)
        stats = import_fns_sme_support_archive(archive_path, dataset_id=dataset_id,
            run_id=run_id, data_date=release.data_date, batch_size=batch_size)
        result = publish_ingestion_run(run_id, stats)
        result.update({'status': 'success', 'source_url': release.data_url,
            'structure_url': release.structure_url, 'sha256': checksum, 'archive_path': str(archive_path)})
    except Exception as error:
        mark_run_failed(run_id, error)
        raise
    if cleanup_old:
        try:
            result['old_rows_deleted'] = cleanup_old_snapshots(dataset_id=dataset_id, keep_run_id=run_id)
            result['cleanup_status'] = 'success'
        except Exception:
            result['old_rows_deleted'] = 0
            result['cleanup_status'] = 'failed'
    else:
        result['old_rows_deleted'] = 0
        result['cleanup_status'] = 'skipped'
    print('W1-003: полный snapshot опубликован, далее проверка и покрытие', flush=True)
    return result


def main():
    import json
    parser = argparse.ArgumentParser(description='Sync official FNS SME support bulk snapshot')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument('--keep-old', action='store_true')
    args = parser.parse_args()
    result = sync_fns_sme_support(force=args.force, batch_size=args.batch_size, cleanup_old=not args.keep_old)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
