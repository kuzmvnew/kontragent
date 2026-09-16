from datetime import date
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from jinja2 import Environment, FileSystemLoader

from app.ingestion.fns_sme_support import parse_support_document, stream_xml_file
from app.services.fns_sme_support_service import _serialize


DAY = date(2026, 9, 15)


def flat(code='1', details=False):
    attrs = {'ИдДок': 'violation-test', 'ДатаСвед': '2026-09-15',
             'СрокПод': '2026-12-31', 'ДатаОказ': '2026-01-01'}
    if code is not None:
        attrs['ИнфНаруш'] = code
    doc = ET.Element('Документ', attrs)
    ET.SubElement(doc, 'ИННЮЛ').text = '7701234567'
    ET.SubElement(doc, 'ОГРН').text = '1027700000001'
    ET.SubElement(doc, 'ФормПод', {'КодФорм': '0100', 'НаимФорм': 'Финансовая поддержка'})
    ET.SubElement(doc, 'ВидПод', {'КодВид': '0103', 'НаимВид': 'Субсидия'})
    ET.SubElement(doc, 'РазмПод', {'РазмПод': '100.00', 'ЕдПод': '1'})
    if details:
        ET.SubElement(doc, 'Нарушения', {'ВидНаруш': '2', 'ДатаНаруш': '2026-02-01'})
    return doc


def normalize(doc):
    return parse_support_document(doc, provider_inn='7707329152', data_date=DAY)


def render(record):
    check = {'result': 'found', 'record_count': 1, 'records': [record], 'data_date': DAY,
             'interpretation_note': '', 'coverage_note': '',
             'official_url': 'https://www.nalog.gov.ru/opendata/7707329152-rsmppp/'}
    return Environment(loader=FileSystemLoader('templates')).get_template(
        'partials/fns_sme_support.html').render(company={'fns_sme_support_check': check})


def test_flag_without_details_is_preserved_not_rejected():
    row = normalize(flat())
    assert row['violation_code'] == '1'
    assert row['violations'] == []
    assert _serialize(SimpleNamespace(**row))['has_violation'] is True


def test_nested_official_flag_without_details_keeps_fact_and_counts():
    fact = flat()
    root = ET.Element('Файл', {'КолДок': '1'})
    doc = ET.SubElement(root, 'Документ', {'ИдДок': 'nested-violation'})
    ET.SubElement(doc, 'СвЮЛ', {'ИННЮЛ': '7701234567', 'ОГРН': '1027700000001'})
    support = ET.SubElement(doc, 'СвПредПод', {
        'НомерПод': '77', 'ИННЮЛ': '7707329152', 'ДатаСвед': '15.09.2026',
        'СрокПод': '31.12.2026', 'ДатаПрин': '01.01.2026', 'ИнфНаруш': '1'})
    support.extend(list(fact)[2:])
    rows = []
    stats = stream_xml_file(BytesIO(ET.tostring(root, encoding='utf-8')),
                            data_date=DAY, consume=rows.append)
    assert stats['source_records'] == stats['eligible_records'] == 1
    assert len(rows) == 1 and rows[0]['violation_code'] == '1'
    assert rows[0]['recipient_inn'] == '7701234567'
    assert rows[0]['provider_inn'] == '7707329152'


def test_browser_template_warns_details_missing_without_clearing_violation():
    html = render(_serialize(SimpleNamespace(**normalize(flat()))))
    assert 'support-violation-flag' in html
    assert 'support-violation-details-missing' in html
    assert 'Отсутствие подробностей не означает отсутствие нарушения' in html
    assert 'В записи ФНС признак нарушения: нет.' not in html


def test_provided_violation_details_still_preserved():
    row = normalize(flat(details=True))
    assert row['violations'][0]['kind_code'] == '2'
    assert row['violations'][0]['violation_date'] == '2026-02-01'
    html = render(_serialize(SimpleNamespace(**row)))
    assert 'support-violation-flag' in html
    assert 'support-violation-details-missing' not in html


@pytest.mark.parametrize('code, expected', [('2', False), (None, None)])
def test_absent_details_do_not_invent_or_suppress_flag(code, expected):
    row = _serialize(SimpleNamespace(**normalize(flat(code=code))))
    assert row['has_violation'] is expected
    if code is None:
        assert 'отсутствие нарушения не подтверждено' in render(row)


def test_invalid_violation_code_still_blocks_import():
    with pytest.raises(ValueError, match='Некорректный код ИнфНаруш'):
        normalize(flat(code='3'))
