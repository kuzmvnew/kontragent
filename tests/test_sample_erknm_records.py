from io import BytesIO
from zipfile import ZipFile

from scripts.sample_erknm_records import (
    flatten_element,
    sample_records_from_zip,
)
from xml.etree import ElementTree as ET


def test_flatten_element_keeps_paths_attributes_and_values():
    element = ET.fromstring(
        """
        <INSPECTION GUID="abc">
            <SUBJECT>
                <INN>7701234567</INN>
                <NAME>ООО ТЕСТ</NAME>
            </SUBJECT>
            <KIND_KNM>Выездная проверка</KIND_KNM>
        </INSPECTION>
        """
    )

    rows = dict(flatten_element(element))

    assert rows["INSPECTION/@GUID"] == "abc"
    assert rows["INSPECTION/SUBJECT/INN"] == "7701234567"
    assert rows["INSPECTION/SUBJECT/NAME"] == "ООО ТЕСТ"
    assert rows["INSPECTION/KIND_KNM"] == "Выездная проверка"


def test_sample_records_from_zip_streams_first_records(tmp_path):
    zip_path = tmp_path / "erknm.zip"
    xml_bytes = b"""<?xml version='1.0' encoding='UTF-8'?>
    <INSPECTIONS>
      <INSPECTION GUID='1'><SUBJECT><INN>1</INN></SUBJECT></INSPECTION>
      <INSPECTION GUID='2'><SUBJECT><INN>2</INN></SUBJECT></INSPECTION>
      <INSPECTION GUID='3'><SUBJECT><INN>3</INN></SUBJECT></INSPECTION>
    </INSPECTIONS>
    """

    with ZipFile(zip_path, "w") as archive:
        archive.writestr("small.xml", b"<x/>")
        archive.writestr("data.xml", xml_bytes)

    member, records = sample_records_from_zip(
        zip_path,
        limit=2,
    )

    assert member == "data.xml"
    assert len(records) == 2
    assert dict(records[0])["INSPECTION/@GUID"] == "1"
    assert dict(records[1])["INSPECTION/SUBJECT/INN"] == "2"
