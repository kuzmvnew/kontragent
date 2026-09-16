"""Full official archive parser proof + small real PostgreSQL/browser regression.

Never claims user-local acceptance or Master Registry coverage. No source XML,
NPD identities, or bulk company data are uploaded as CI artifacts.
"""
from __future__ import annotations

import io
import json
import os
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx
from sqlalchemy import select, func

from app.ingestion.fns_sme_support import stream_xml_file, parse_support_document
from app.ingestion.fns_sme_support_integrity import count_xml, sha256_file
from app.ingestion.fns_sme_support_xml import parse_support_records
from app.providers.fns_sme_support_provider import FnsSmeSupportProvider, FnsSmeSupportRelease

OUT = Path('data/acceptance/w1-003-full-parser')
DATA_URL = 'https://file.nalog.ru/opendata/7707329152-rsmppp/data-20260915-structure-20230615.zip'
XSD_URL = 'https://file.nalog.ru/opendata/7707329152-rsmppp/structure-20230615.xsd'
TARGET_MEMBER = 'VO_SVMSP_0000_9965_20260915_03ce0b33-b0a9-4670-81f4-c0fa81433098.xml'
TARGET_DOCUMENT = '78088af2-495c-409b-8d7d-fe5c1f910517'
DAY = date(2026, 9, 15)
ARCHIVE = None


def initialize_worker(path):
    global ARCHIVE
    ARCHIVE = ZipFile(path)


def scan_member(name):
    try:
        raw = ARCHIVE.read(name)  # Reads to EOF and verifies this member's CRC.
        proof = count_xml(io.BytesIO(raw))
        counts = Counter()
        def consume(row):
            if row['violation_code'] == '1':
                counts['violation_flagged_records'] += 1
                if not row['violations']:
                    counts['violation_missing_details_records'] += 1
        stats = stream_xml_file(io.BytesIO(raw), data_date=DAY, consume=consume,
                                verified_counts=proof)
        return {'ok': True, 'member': name, 'stats': stats, 'quality': dict(counts),
                'header_mismatch': proof['header_count_check'] == 'MISMATCH',
                'date': proof['data_date']}
    except Exception as error:
        return {'ok': False, 'member': name, 'error': str(error)[:700],
                'error_type': type(error).__name__}


def real_record_regression(path):
    from app.database.base import Base
    from app.database.postgres import engine, get_session
    import app.models  # noqa: F401
    from app.models.company import Company
    from app.models.fns_sme_support import FnsSmeSupportEntry as Entry
    from app.services.fns_sme_support_service import get_fns_sme_support_check_for_inn
    from scripts.sync_fns_sme_support import sync_fns_sme_support
    from scripts.accept_fns_sme_support import browser_check

    assert os.getenv('GITHUB_ACTIONS') == 'true'
    assert engine.url.database == 'test' and engine.url.host == 'localhost'
    with ZipFile(path) as archive:
        raw = archive.read(TARGET_MEMBER)
    root = ET.fromstring(raw)
    doc = next(d for d in root if d.get('ИдДок') == TARGET_DOCUMENT)
    rows = list(parse_support_records(doc, data_date=DAY, provider_inn=None,
                                     legacy_parser=parse_support_document))
    missing = [r for r in rows if r['violation_code'] == '1' and not r['violations']]
    assert missing, 'The exact Mac failure must remain a flagged record without details'
    target = missing[0]
    identity = next(e for e in doc if e.tag.rsplit('}', 1)[-1] in {'СвЮЛ', 'СвФЛ'})
    legal = identity.tag.rsplit('}', 1)[-1] == 'СвЮЛ'
    # Use the legal entity from the exact failing document. If the source record
    # is an IP, private identity strings are not printed or uploaded.
    name = identity.get('НаимОрг') if legal else 'ИП — сведения из официальной выгрузки ФНС'
    assert target['recipient_kind'] != 'npd_individual'
    subset = OUT / 'real-regression-subset.zip'
    with ZipFile(subset, 'w') as archive:
        archive.writestr(TARGET_MEMBER, raw)
    Base.metadata.create_all(engine)
    with get_session() as session:
        session.add(Company(inn=target['recipient_inn'], name=name,
                            entity_type='legal' if legal else 'individual'))
        session.commit()
    unavailable = browser_check([{'label': 'before-import-unavailable',
        'inn': target['recipient_inn'], 'result': 'unavailable'}], OUT)
    imported = sync_fns_sme_support(archive_path=subset,
        release=FnsSmeSupportRelease('https://fixtures.invalid/real-violation-subset.zip',
                                     XSD_URL, None, DAY), cleanup_old=False)
    with get_session() as session:
        stored = session.scalar(select(Entry).where(
            Entry.source_record_key == target['source_record_key']))
        assert stored is not None and stored.violation_code == '1'
        assert stored.violations == []
        missing_count = session.scalar(select(func.count()).select_from(Entry).where(
            Entry.violation_code == '1', func.jsonb_array_length(Entry.violations) == 0))
    check = get_fns_sme_support_check_for_inn(target['recipient_inn'])
    assert check['result'] == 'found'
    assert any(r['has_violation'] is True and not r['violations'] for r in check['records'])
    pages = browser_check([{'label': 'real-violation-missing-details',
        'inn': target['recipient_inn'], 'result': 'found',
        'text': 'Отсутствие подробностей не означает отсутствие нарушения'}], OUT)
    assert imported['source_quality']['violation_missing_details_records'] == missing_count
    return {'status': 'PASS_REAL_FAILING_MEMBER_ONLY', 'member': TARGET_MEMBER,
        'document_id': TARGET_DOCUMENT, 'company': name if legal else 'IP identity not disclosed',
        'inn': target['recipient_inn'] if legal else None,
        'persisted_records': imported['persisted_records'], 'source_quality': imported['source_quality'],
        'violation_flag_preserved': True, 'postgres_reread': True,
        'browser': unavailable + pages, 'full_postgresql_import': 'NOT_RUN',
        'user_mac': 'NOT_ACCESSED', 'user_master_coverage': 'NOT_CONFIRMED'}


def main():
    if os.getenv('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('This regression writes only to disposable GitHub-hosted CI')
    OUT.mkdir(parents=True, exist_ok=True)
    report = {'executed_at': datetime.now(timezone.utc).isoformat(),
              'source_url': DATA_URL, 'user_local_six_gates': 'NOT_CONFIRMED'}
    try:
        with httpx.Client(timeout=45, follow_redirects=True) as client:
            response = client.get(XSD_URL)
            response.raise_for_status()
            schema = ET.fromstring(response.content)
        declaration = schema.find(".//{http://www.w3.org/2001/XMLSchema}element[@name='Нарушения']")
        assert declaration is not None and declaration.get('minOccurs') == '0'
        report['official_schema'] = {'url': XSD_URL, 'http_status': response.status_code,
            'violations_min_occurs': declaration.get('minOccurs'),
            'sha256': __import__('hashlib').sha256(response.content).hexdigest()}
        path = OUT / 'official.zip'
        print('Downloading ONE official full ZIP for parser validation', flush=True)
        download = FnsSmeSupportProvider().download(
            FnsSmeSupportRelease(DATA_URL, XSD_URL, None, DAY), path)
        report['download'] = {k: v for k, v in download.items() if k != 'path'}
        # Reproduce and round-trip the exact failing record before the bulk scan.
        report['real_record'] = real_record_regression(path)
        print(json.dumps({'real_record': report['real_record']}, ensure_ascii=False, default=str), flush=True)
        with ZipFile(path) as archive:
            members = [m.filename for m in archive.infolist() if not m.is_dir()]
        assert len(members) == len(set(members))
        names = [name for name in members if name.lower().endswith('.xml')]
        assert names
        totals = Counter()
        quality = Counter()
        errors = []
        failed = 0
        dates = set()
        mismatches = 0
        with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1),
                initializer=initialize_worker, initargs=(str(path),)) as pool:
            for index, result in enumerate(pool.map(scan_member, names, chunksize=20), 1):
                if not result['ok']:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(result)
                else:
                    stats = result['stats']
                    for key in ('source_records', 'eligible_records', 'excluded_npd_records'):
                        totals[key] += stats[key]
                    totals['source_documents'] += stats.get('source_documents', stats['source_records'])
                    quality.update(result['quality'])
                    dates.add(result['date'])
                    mismatches += int(result['header_mismatch'])
                if index == 1 or index % 500 == 0 or index == len(names):
                    print(f'FULL PARSER: {index}/{len(names)} XML; failures={failed}; facts={totals["source_records"]}', flush=True)
        # Drain any non-XML member too; no archive member is silently unverified.
        with ZipFile(path) as archive:
            for name in members:
                if not name.lower().endswith('.xml'):
                    with archive.open(name) as stream:
                        for _ in iter(lambda: stream.read(1024 * 1024), b''):
                            pass
        report['full_parser'] = {'xml_files': len(names), 'failed_files': failed,
            'successful_files': len(names) - failed, 'totals': dict(totals),
            'source_quality': dict(quality), 'snapshot_dates': sorted(str(d) for d in dates),
            'header_count_mismatch_files': mismatches, 'errors': errors,
            'crc_eof_all_members': failed == 0,
            'archive_sha256_unchanged': sha256_file(path) == download['sha256'],
            'full_postgresql_import': 'NOT_RUN', 'duplicate_check': 'NOT_CONFIRMED'}
        assert not failed, f'{failed} XML members failed full parser validation'
        assert totals['source_records'] == totals['eligible_records'] + totals['excluded_npd_records']
        assert dates == {'2026-09-15'}
        assert report['full_parser']['archive_sha256_unchanged']
        report['status'] = 'PASS_FULL_ARCHIVE_PARSER_AND_REAL_RECORD_REGRESSION'
        code = 0
    except Exception as error:
        report['status'] = 'FAIL'
        report['error_type'] = type(error).__name__
        report['error'] = str(error)[:1500]
        code = 1
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
