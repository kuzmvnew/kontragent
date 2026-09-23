from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.ingestion import fns_tax_debt_pipeline as pipeline
from app.providers.fns_tax_debt_provider import (
    TaxDebtDiscoveryError,
    discover_tax_debt_release,
    parse_tax_debt_discovery_page,
)
from app.sources.fns_tax_debt import OFFICIAL_SOURCE_PAGE, PILOT_ENVIRONMENT
from app.worker.errors import LegalBlockError


DISCOVERED_AT = datetime(2026, 9, 23, 9, tzinfo=timezone.utc)


def _page(
    *,
    artifact_date: str = "20260825",
    modified: str = "25.08.2026",
    data_as_of: str = "01.08.2026",
    actual_until: str = "25.09.2026",
) -> str:
    return f"""
    <html><body><table>
      <tr><td>1</td><td>Идентификационный номер</td><td>7707329152-debtam</td></tr>
      <tr><td>8</td><td>Гиперссылка (URL) на набор</td><td>
        <a href="https://file.nalog.ru/opendata/7707329152-debtam/data-{artifact_date}-structure-20181201.zip">ZIP</a>
      </td></tr>
      <tr><td>10</td><td>Описание структуры набора данных</td><td>
        <a href="https://file.nalog.ru/opendata/7707329152-debtam/structure-20181201.xsd">XSD</a>
      </td></tr>
      <tr><td>12</td><td>Дата последнего внесения изменений</td><td>{modified}</td></tr>
      <tr><td>13</td><td>Содержание последнего изменения</td><td>Данные на {data_as_of}</td></tr>
      <tr><td>14</td><td>Дата актуальности</td><td>{actual_until}</td></tr>
    </table></body></html>
    """


def _valid_inns(count: int) -> frozenset[str]:
    values: list[str] = []
    candidate = 1
    while len(values) < count:
        base = f"{candidate:09d}"
        digits = [int(char) for char in base]
        check = sum(
            value * weight
            for value, weight in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))
        ) % 11 % 10
        inn = base + str(check)
        if pipeline.is_valid_legal_entity_inn(inn):
            values.append(inn)
        candidate += 1
    return frozenset(values)


def _xsd(path: Path) -> Path:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="Файл">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="Документ" minOccurs="1" maxOccurs="unbounded">
                  <xs:complexType>
                    <xs:sequence>
                      <xs:element name="СведНП">
                        <xs:complexType>
                          <xs:attribute name="ИННЮЛ" type="xs:string" use="required"/>
                          <xs:attribute name="НаимОрг" type="xs:string"/>
                        </xs:complexType>
                      </xs:element>
                      <xs:element name="СведНедоим" minOccurs="1" maxOccurs="unbounded">
                        <xs:complexType>
                          <xs:attribute name="НаимНалог" type="xs:string"/>
                          <xs:attribute name="СумНедНалог" type="xs:decimal"/>
                          <xs:attribute name="СумПени" type="xs:decimal"/>
                          <xs:attribute name="СумШтраф" type="xs:decimal"/>
                          <xs:attribute name="ОбщСумНедоим" type="xs:decimal" use="required"/>
                        </xs:complexType>
                      </xs:element>
                    </xs:sequence>
                    <xs:attribute name="ИдДок" type="xs:string" use="required"/>
                    <xs:attribute name="ДатаДок" type="xs:string" use="required"/>
                    <xs:attribute name="ДатаСост" type="xs:string" use="required"/>
                  </xs:complexType>
                </xs:element>
              </xs:sequence>
              <xs:attribute name="ВерсФорм" type="xs:string" use="required"/>
              <xs:attribute name="ИдФайл" type="xs:string" use="required"/>
              <xs:attribute name="ТипИнф" type="xs:string" use="required"/>
              <xs:attribute name="КолДок" type="xs:integer" use="required"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """,
        encoding="utf-8",
    )
    return path


def test_discovery_parses_only_explicit_official_artifact_and_xsd_urls():
    release = parse_tax_debt_discovery_page(_page())

    assert release.discovery_page_url == OFFICIAL_SOURCE_PAGE
    assert release.artifact_url.endswith("data-20260825-structure-20181201.zip")
    assert release.xsd_url.endswith("structure-20181201.xsd")
    assert release.source_as_of == datetime(2026, 8, 25, tzinfo=timezone.utc)
    assert release.data_as_of == date(2026, 8, 1)
    assert release.official_actual_until == date(2026, 9, 25)


def test_discovery_distinguishes_changed_artifact_from_no_change():
    first = discover_tax_debt_release(_page(), discovered_at=DISCOVERED_AT)
    unchanged = discover_tax_debt_release(
        _page(),
        discovered_at=DISCOVERED_AT,
        previous=first.release,
    )
    changed = discover_tax_debt_release(
        _page(
            artifact_date="20260920",
            modified="20.09.2026",
            data_as_of="01.09.2026",
            actual_until="20.10.2026",
        ),
        discovered_at=DISCOVERED_AT,
        previous=first.release,
    )

    assert first.changed is True
    assert unchanged.changed is False
    assert changed.changed is True


def test_discovery_never_guesses_missing_xsd_url():
    page = _page().replace(
        '<a href="https://file.nalog.ru/opendata/7707329152-debtam/structure-20181201.xsd">XSD</a>',
        "XSD omitted",
    )

    with pytest.raises(TaxDebtDiscoveryError, match="exact XSD URL"):
        parse_tax_debt_discovery_page(page)


def test_pilot_cohort_over_100_is_blocked_and_default_is_disabled():
    with pytest.raises(LegalBlockError, match="disabled"):
        pipeline.ControlledLivePilotConfig(cohort_inns=_valid_inns(1)).validate()

    with pytest.raises(LegalBlockError, match="exceeds 100"):
        pipeline.ControlledLivePilotConfig(
            enabled=True,
            environment=PILOT_ENVIRONMENT,
            cohort_inns=_valid_inns(101),
        ).validate()


def test_publication_outside_cohort_is_fail_closed():
    cohort_inn, outside_inn = tuple(_valid_inns(2))

    with pytest.raises(LegalBlockError, match="outside the approved cohort"):
        pipeline._assert_controlled_live_entries(
            ({"inn": outside_inn},),
            manifest={"cohort_inns": [cohort_inn]},
        )


def test_pinned_xsd_validation_stops_invalid_xml(tmp_path):
    xsd = _xsd(tmp_path / "structure.xsd")
    source = tmp_path / "invalid-xsd.zip"
    from zipfile import ZipFile

    xml = """
    <Файл ВерсФорм="4.01" ИдФайл="fixture" ТипИнф="ОТКРДАННЫЕ6" КолДок="1">
      <Документ ИдДок="D-1" ДатаДок="25.08.2026" ДатаСост="01.08.2026">
        <НеСведНП ИННЮЛ="7707083893" />
        <СведНедоим ОбщСумНедоим="0.00" />
      </Документ>
    </Файл>
    """
    with ZipFile(source, "w") as archive:
        archive.writestr("data.xml", xml.encode("utf-8"))

    with pytest.raises(pipeline.TaxDebtSchemaError, match="pinned XSD validation"):
        pipeline.parse_tax_debt_zip(source, xsd_path=xsd)


def test_stale_official_actual_until_is_explicit():
    assert pipeline.release_freshness(
        date(2026, 9, 22),
        now=DISCOVERED_AT,
    ) == "stale"
    assert pipeline.release_freshness(
        date(2026, 9, 23),
        now=DISCOVERED_AT,
    ) == "current"
