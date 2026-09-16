from datetime import date
from io import BytesIO
from xml.etree import ElementTree as ET

import pytest

from app.ingestion.fns_sme_support import stream_xml_file


def xml(recipient='<СвЮЛ ИННЮЛ="7707083893" ОГРН="1027700132195" НаимОрг="TEST"/>', count=1):
    return f'''<Файл КолДок="{count}" ВерсФорм="4.04"><Документ ИдДок="DOC-1" ДатаСост="15.08.2026">
    {recipient}
    <СвПредПод НомерПод="SUP-1" ИННЮЛ="7707329152" ДатаСвед="20.12.2020" СрокПод="05.06.2020" ДатаПрин="05.06.2020" ИнфНаруш="2">
      <ФормПод КодФорм="0100" НаимФорм="Финансовая поддержка"/>
      <ВидПод КодВид="0103" НаимВид="Предоставление субсидий и грантов"/>
      <РазмПод РазмПод="12130.00" ЕдПод="1"/>
    </СвПредПод>
    <СвПредПод НомерПод="SUP-2" ИННЮЛ="7707329152" ДатаСвед="15.01.2022" СрокПод="06.12.2021" ДатаПрин="06.12.2021" ИнфНаруш="2">
      <ФормПод КодФорм="0100" НаимФорм="Финансовая поддержка"/>
      <ВидПод КодВид="0103" НаимВид="Предоставление субсидий и грантов"/>
      <РазмПод РазмПод="12792.00" ЕдПод="1"/>
    </СвПредПод></Документ></Файл>'''.encode('utf8')


def parse(raw):
    rows = []
    stats = stream_xml_file(BytesIO(raw), data_date=date(2026, 9, 15), consume=rows.append)
    return rows, stats


def test_open_data_recipient_and_provider_are_separate():
    rows, stats = parse(xml())
    assert len(rows) == 2
    assert {r['recipient_inn'] for r in rows} == {'7707083893'}
    assert {r['provider_inn'] for r in rows} == {'7707329152'}
    assert len({r['source_record_key'] for r in rows}) == 2
    assert rows[0]['decision_date'] == date(2020, 6, 5)
    assert rows[1]['decision_date'] == date(2021, 12, 6)
    assert stats['source_documents'] == stats['expected_documents'] == 1
    assert stats['source_records'] == stats['eligible_records'] == 2


def test_ip_nested_identifiers():
    rows, _ = parse(xml('<СвФЛ ИННФЛ="770123456789" ОГРНИП="312621501700051"/>'))
    assert all(r['recipient_kind'] == 'individual_entrepreneur_or_kfh' for r in rows)
    assert all(r['recipient_inn'] == '770123456789' for r in rows)


def test_npd_facts_counted_but_not_stored():
    rows, stats = parse(xml('<СвФЛ ИННФЛ="770123456789"/>'))
    assert rows == []
    assert stats['source_documents'] == 1
    assert stats['excluded_npd_records'] == 2
    assert stats['eligible_records'] == 0


def test_provider_inn_must_never_substitute_missing_recipient():
    with pytest.raises(ValueError, match='ИНН именно получателя'):
        parse(xml('<СвЮЛ НаимОрг="TEST" ОГРН="1027700132195"/>'))


def test_unknown_recipient_shape_fails_closed():
    with pytest.raises(ValueError, match='СвЮЛ/СвФЛ'):
        parse(xml('<Unknown ИННЮЛ="7707083893"/>'))


def test_document_count_is_not_support_fact_count():
    with pytest.raises(ValueError, match='КолДок=2'):
        parse(xml(count=2))


def test_official_decision_date_is_required_not_replaced_with_snapshot_date():
    with pytest.raises(ValueError, match='обязательные даты'):
        parse(xml().replace('ДатаПрин="05.06.2020"'.encode(), b''))


def test_namespace_aware_nested_shape():
    root = ET.fromstring(xml())
    for element in root.iter():
        element.tag = '{urn:fns:test}' + element.tag
    rows, stats = parse(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    assert len(rows) == 2 and stats['expected_documents'] == 1
