from datetime import date
from zipfile import ZipFile

from app.ingestion.erknm import (
    iter_erknm_records,
    normalize_inn,
    normalize_ogrn,
    parse_inspection_element,
)
from scripts.import_erknm_inspections import infer_data_date
import xml.etree.ElementTree as ET


SAMPLE_INSPECTION = """
<INSPECTION
    CLASSIFICATION="ПМ"
    CREATION_SOURCE="Создано через ФГИС ЕРКНМ"
    STATUS="Предостережение объявлено"
    STATUS_KEY="REMARK"
    ERPID="77260061000120428959"
    KO_LEVEL="FEDERAL"
    SUPERVISION_ID="Федеральный государственный пожарный надзор"
    START_DATE="2026-01-04">
  <PROSECUTOR_OFFICE NAME="Прокуратура города Москвы"/>
  <KIND_CONTROL VALUE="Федеральный государственный пожарный надзор"/>
  <KIND_KNM VALUE="Объявление предостережения"/>
  <SUBJECT
      INN="7701364231"
      OGRN="1137746586251"
      NAME="ООО ТЕСТ"
      TYPE="ЮЛ"
      GUID="subject-guid"
      MSP_CODE="Микропредприятие">
    <OKVEDS CODE="68.32" NAME="Управление недвижимым имуществом"/>
  </SUBJECT>
  <OBJECT ADDRESS="г Москва" GUID="object-guid">
    <OBJECT_TYPE VALUE="Производственные объекты"/>
    <OBJECT_KIND VALUE="здания"/>
    <OBJECT_SUB_KIND VALUE="здания"/>
    <RISK_CATEGORY VALUE="значительный риск"/>
  </OBJECT>
  <INSPECTOR INSPECTOR_FULL_NAME="Иванов Иван" GUID="inspector-guid">
    <INSPECTOR_POSITION VALUE="Государственный инспектор"/>
  </INSPECTOR>
  <KNO_ORGANIZATION VALUE="ГУ МЧС ПО Г. МОСКВЕ"/>
  <REASON_RISK>
    <REASON TEXT="ФЗ №248-ФЗ" MAIN="false" APPROVE_REQUIRED="false">
      <REASON_TYPE HAS_TEXT="true" APPROVE_REQUIRED="false" DATE_REQUIRED="false"/>
    </REASON>
  </REASON_RISK>
  <WARNING_INFO>
    <CAPTION>Текст предостережения</CAPTION>
  </WARNING_INFO>
  <ACTIONS_INFO>
    <RESULT>Нарушений не выявлено</RESULT>
  </ACTIONS_INFO>
</INSPECTION>
""".strip()


def test_normalize_identifiers():
    assert normalize_inn("7701364231") == "7701364231"
    assert normalize_inn("123") is None
    assert normalize_ogrn("1137746586251") == "1137746586251"
    assert normalize_ogrn("bad") is None


def test_parse_inspection_element():
    element = ET.fromstring(SAMPLE_INSPECTION)

    record = parse_inspection_element(
        element,
        data_date=date(2026, 9, 15),
        period_year=2026,
        period_month=1,
    )

    assert record is not None
    assert record["erpid"] == "77260061000120428959"
    assert record["classification"] == "ПМ"
    assert record["status_key"] == "REMARK"
    assert record["start_date"] == date(2026, 1, 4)
    assert record["subject_inn"] == "7701364231"
    assert record["subject_ogrn"] == "1137746586251"
    assert record["subject_name"] == "ООО ТЕСТ"
    assert record["kind_knm"] == "Объявление предостережения"
    assert record["kno_organization"] == "ГУ МЧС ПО Г. МОСКВЕ"
    assert record["risk_category"] == "значительный риск"
    assert record["reason_text"] == "ФЗ №248-ФЗ"
    assert record["warning_caption"] == "Текст предостережения"
    assert record["result_text"] == "Нарушений не выявлено"
    assert record["okveds"][0]["CODE"] == "68.32"
    assert record["objects"][0]["address"] == "г Москва"
    assert record["inspectors"][0]["attributes"]["INSPECTOR_FULL_NAME"] == "Иванов Иван"


def test_iter_erknm_records_streams_zip(tmp_path):
    zip_path = tmp_path / "data-20260915-structure-20220125.zip"
    xml = f"<INSPECTIONS>{SAMPLE_INSPECTION}{SAMPLE_INSPECTION.replace('77260061000120428959', '77260061000120428960')}</INSPECTIONS>"

    with ZipFile(zip_path, "w") as archive:
        archive.writestr("data.xml", xml)

    records = list(
        iter_erknm_records(
            zip_path,
            data_date=date(2026, 9, 15),
            period_year=2026,
            period_month=1,
        )
    )

    assert len(records) == 2
    assert records[0]["erpid"] == "77260061000120428959"
    assert records[1]["erpid"] == "77260061000120428960"


def test_iter_erknm_records_limit(tmp_path):
    zip_path = tmp_path / "data-20260915-structure-20220125.zip"
    xml = f"<INSPECTIONS>{SAMPLE_INSPECTION}{SAMPLE_INSPECTION.replace('77260061000120428959', '77260061000120428960')}</INSPECTIONS>"

    with ZipFile(zip_path, "w") as archive:
        archive.writestr("data.xml", xml)

    records = list(
        iter_erknm_records(
            zip_path,
            data_date=date(2026, 9, 15),
            period_year=2026,
            period_month=1,
            limit=1,
        )
    )

    assert len(records) == 1


def test_infer_data_date_from_archive_name(tmp_path):
    path = tmp_path / "data-20260915-structure-20220125.zip"
    assert infer_data_date(path) == date(2026, 9, 15)
