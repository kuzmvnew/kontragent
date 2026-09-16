"""FNS open-data 4.04: one recipient document contains many support facts.

Verified against structure-20230615.xsd and live bulk XML. The flat shape is
retained only for compatibility with the previous exchange-format fixtures.
"""
from copy import deepcopy
from xml.etree import ElementTree as ET


def _name(element):
    return str(element.tag).rsplit('}', 1)[-1]


def parse_support_records(document, *, data_date, provider_inn, legacy_parser):
    recipients = [x for x in document if _name(x) in {'СвЮЛ', 'СвФЛ'}]
    supports = [x for x in document if _name(x) == 'СвПредПод']
    if not recipients and not supports:
        yield legacy_parser(document, provider_inn=provider_inn, data_date=data_date)
        return
    if len(recipients) != 1 or not supports:
        raise ValueError('Ожидался один СвЮЛ/СвФЛ и хотя бы один СвПредПод')
    recipient = recipients[0]
    inn_key, ogrn_key, inn_len, ogrn_len = (
        ('ИННЮЛ', 'ОГРН', 10, 13) if _name(recipient) == 'СвЮЛ'
        else ('ИННФЛ', 'ОГРНИП', 12, 15)
    )
    inn = recipient.get(inn_key, '').strip()
    ogrn = recipient.get(ogrn_key, '').strip()
    if not inn.isascii() or not inn.isdigit() or len(inn) != inn_len:
        raise ValueError('Некорректный ИНН именно получателя в СвЮЛ/СвФЛ')
    if (ogrn and (not ogrn.isascii() or not ogrn.isdigit() or len(ogrn) != ogrn_len)) or (inn_len == 10 and not ogrn):
        raise ValueError('Некорректный ОГРН/ОГРНИП получателя')
    document_id = document.get('ИдДок', '').strip()
    if not document_id or len(document_id) > 100:
        raise ValueError('Некорректный ИдДок ФНС')
    for support in supports:
        number = support.get('НомерПод', '').strip()
        giver_inn = support.get('ИННЮЛ', '').strip()
        if not number or len(number) > 36:
            raise ValueError('У СвПредПод отсутствует корректный НомерПод')
        if not giver_inn.isascii() or not giver_inn.isdigit() or len(giver_inn) != 10:
            raise ValueError('У СвПредПод отсутствует ИНН организации, оказавшей поддержку')
        # Never search recursively for INN: support.INN identifies the giver.
        flat = ET.Element('Документ', {
            'ИдДок': document_id,
            'ДатаСвед': support.get('ДатаСвед', ''),
            'СрокПод': support.get('СрокПод', ''),
            'ДатаОказ': support.get('ДатаПрин', ''),
            'ДатаПрекр': support.get('ДатаПрекр', ''),
            'ИнфНаруш': support.get('ИнфНаруш', ''),
        })
        ET.SubElement(flat, inn_key).text = inn
        if ogrn:
            ET.SubElement(flat, ogrn_key).text = ogrn
        for child in support:
            if _name(child) in {'ФормПод', 'ВидПод', 'РазмПод', 'Нарушения', 'РегДок'}:
                flat.append(deepcopy(child))
        record = legacy_parser(flat, provider_inn=giver_inn, data_date=data_date)
        if not all(record.get(key) for key in (
            'support_form_code', 'support_form_name', 'support_type_code', 'support_type_name'
        )):
            raise ValueError('У СвПредПод отсутствует форма или вид поддержки')
        record['source_record_key'] = f'{giver_inn}:{document_id}:{number}:{inn}'
        yield record
