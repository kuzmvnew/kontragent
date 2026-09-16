from datetime import date
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_STORED

import pytest

from app.ingestion.fns_sme_support_integrity import audit_archive, count_xml, verify_counts
from app.ingestion.fns_sme_support import stream_xml_file, publish_ingestion_run


def source_xml(count=1, declared=1, version='4.04', missing_date=False):
    docs = []
    for i in range(count):
        data_date = '' if missing_date else ' ДатаСост="15.09.2026"'
        docs.append(f'''<Документ ИдДок="doc-{i}"{data_date}>
          <СвЮЛ ИННЮЛ="7701234567" ОГРН="1027700000001" НаимОрг="Fixture"/>
          <СвПредПод НомерПод="{i}" ИННЮЛ="7707329152" ДатаСвед="01.02.2026" СрокПод="31.12.2026" ДатаПрин="01.01.2026" ИнфНаруш="2">
            <ФормПод КодФорм="0100" НаимФорм="Финансовая поддержка"/>
            <ВидПод КодВид="0103" НаимВид="Субсидия"/>
            <РазмПод РазмПод="100.00" ЕдПод="1"/>
          </СвПредПод></Документ>''')
    return (f'<?xml version="1.0" encoding="windows-1251"?><Файл КолДок="{declared}" ВерсФорм="{version}" ТипИнф="РЕЕСТРМСП-ПП">'
            + ''.join(docs) + '</Файл>').encode('cp1251')


def archive(tmp_path, raw):
    path = tmp_path/'data.zip'
    with ZipFile(path, 'w', compression=ZIP_STORED) as z:
        z.writestr('data.xml', raw)
    return path


def test_known_header_mismatch_is_reported_not_called_pass(tmp_path):
    proof = audit_archive(archive(tmp_path, source_xml(3)))
    summary = proof['summary']
    assert summary['header_count_check'] == 'MISMATCH'
    assert summary['header_count_mismatch_files'] == 1
    assert summary['independent_counts']['source_documents'] == 3
    assert summary['independent_counts']['declared_documents'] == 1
    assert summary['data_date'] == '2026-09-15'
    rows = []
    stream_xml_file(BytesIO(source_xml(3)), data_date=date(2026, 9, 15), consume=rows.append,
                    verified_counts=proof['members']['data.xml'])
    assert len(rows) == 3
    assert len({row['source_record_key'] for row in rows}) == 3


def test_standalone_parser_remains_strict():
    with pytest.raises(ValueError, match='КолДок'):
        stream_xml_file(BytesIO(source_xml(2)), data_date=date(2026, 9, 15), consume=lambda row: None)


def test_unknown_mismatch_rejected():
    with pytest.raises(ValueError, match='Unrecognized'):
        count_xml(BytesIO(source_xml(3, declared=2)))


def test_other_version_mismatch_rejected():
    with pytest.raises(ValueError, match='Unrecognized'):
        count_xml(BytesIO(source_xml(3, version='5.00')))


def test_truncated_xml_rejected(tmp_path):
    with pytest.raises(ValueError):
        audit_archive(archive(tmp_path, source_xml(3)[:-12]))


def test_corrupt_crc_rejected(tmp_path):
    path = archive(tmp_path, source_xml())
    raw = path.read_bytes()
    assert b'100.00' in raw
    path.write_bytes(raw.replace(b'100.00', b'200.00', 1))
    with pytest.raises(ValueError, match='integrity'):
        audit_archive(path)


def test_independent_count_detects_dropped_fact():
    proof = count_xml(BytesIO(source_xml(2)))
    with pytest.raises(ValueError, match='support fact'):
        verify_counts({'source_documents': 2, 'source_records': 1}, proof)


def test_missing_official_snapshot_date_rejected():
    with pytest.raises(ValueError, match='ДатаСост'):
        count_xml(BytesIO(source_xml(missing_date=True)))


def test_cannot_publish_without_full_archive_evidence():
    with pytest.raises(ValueError, match='integrity evidence'):
        publish_ingestion_run(123, {'source_records': 5})


def test_duplicate_members_rejected(tmp_path):
    path = archive(tmp_path, source_xml())
    with pytest.warns(UserWarning):
        with ZipFile(path, 'a') as z:
            z.writestr('data.xml', source_xml())
    with pytest.raises(ValueError, match='Duplicate ZIP'):
        audit_archive(path)
