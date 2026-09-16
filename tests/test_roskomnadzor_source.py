from datetime import date
import io

from jinja2 import Environment, FileSystemLoader
from openpyxl import Workbook
import pytest

from app.ingestion.roskomnadzor import parse_hosting_xlsx, parse_xml_snapshot, validate_complete_snapshot
from app.providers.roskomnadzor_provider import PD_OPERATOR_URL, RoskomnadzorPdOperatorProvider, RoskomnadzorProviderError, parse_pd_operator_html
from app.services.roskomnadzor_registry_service import build_roskomnadzor_dataset_specs, build_roskomnadzor_source_spec
from app.services.roskomnadzor_service import get_roskomnadzor_bulk_check_for_inn


XML = b'''<?xml version="1.0" encoding="utf-8"?>
<root>
 <record><inn>7701234567</inn><ogrn>1027700000000</ogrn><number>LIC-1</number><name>Company</name><status>active</status><email>secret@example.test</email><phone>123</phone><territory>Moscow</territory></record>
 <record><inn>770123456789</inn><ogrn>315770000000001</ogrn><number>LIC-IP</number><name>Private person</name><email>person@example.test</email></record>
 <record><number>NO-ID</number><name>Named person</name></record>
</root>'''


class Response:
    status_code = 200
    text = "<html><body>ИНН 7701234567 Регистрационный номер 77-26-562985</body></html>"


class Client:
    def __init__(self): self.calls = []
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return Response()


def test_xml_parser_enforces_public_private_boundary():
    parsed = parse_xml_snapshot(XML, channel="communications", data_date=date(2026, 9, 15))
    validate_complete_snapshot(parsed)
    assert parsed["source_records"] == 3
    assert len(parsed["public_records"]) == 1
    assert len(parsed["private_records"]) == 2
    public = parsed["public_records"][0]
    assert public["inn"] == "7701234567" and public["ogrn"] == "1027700000000"
    assert "email" not in public["public_details"] and "phone" not in public["public_details"]
    assert public["public_details"] == {"territory": "Moscow"}
    assert all(row["is_published"] is False for row in parsed["private_records"])


def test_truncated_xml_is_never_publishable_and_channel_stays_unavailable(monkeypatch):
    with pytest.raises(ValueError, match="неполный XML"):
        parse_xml_snapshot(XML[:-10], channel="communications", data_date=date(2026, 9, 15))

    class Session:
        def scalar(self, statement):
            return None

        def close(self):
            pass

    monkeypatch.setattr("app.services.roskomnadzor_service.get_session", Session)
    result = get_roskomnadzor_bulk_check_for_inn("7701234567", "broadcast")
    assert result["result"] == "unavailable"
    assert result["reason"] == "dataset_not_loaded"
    assert result["checked"] is False


def test_hosting_xlsx_routes_twelve_digit_inn_to_private_layer():
    workbook = Workbook(); sheet = workbook.active
    sheet.append(["№ в реестре", "Наименование", "ИНН", "ОГРН", "Контактное лицо"])
    sheet.append(["H-1", "Company", "7701234567", "1027700000000", "Person One"])
    sheet.append(["H-2", "IP", "770123456789", "315770000000001", "Person Two"])
    stream = io.BytesIO(); workbook.save(stream)
    parsed = parse_hosting_xlsx(stream.getvalue(), data_date=date(2026, 7, 30))
    assert len(parsed["public_records"]) == 1 and len(parsed["private_records"]) == 1
    assert "контактное лицо" not in parsed["public_records"][0]["public_details"]


def test_pd_html_requires_exact_inn_and_strips_personal_details():
    html = "<html><body>ООО Банк, ИНН 9706063520, 77-26-562985; Ответственный Иванов, phone 123</body></html>"
    parsed = parse_pd_operator_html(html, "9706063520")
    assert parsed == {"found": True, "records": [{"inn": "9706063520", "registry_number": "77-26-562985"}], "total": 1}
    assert "Иванов" not in str(parsed) and "123" not in str(parsed)


def test_pd_html_distinguishes_empty_and_invalid_response():
    assert parse_pd_operator_html("<p>По вашему запросу ничего не найдено</p>", "9102309919")["found"] is False
    with pytest.raises(RoskomnadzorProviderError) as error:
        parse_pd_operator_html("<p>Форма изменилась</p>", "9102309919")
    assert error.value.kind == "invalid_response"


def test_pd_provider_makes_one_official_exact_query():
    client = Client(); result = RoskomnadzorPdOperatorProvider(client=client).check_inn("7701234567")
    assert result["found"] is True and len(client.calls) == 1
    assert client.calls[0][0] == PD_OPERATOR_URL
    assert client.calls[0][1]["params"]["inn"] == "7701234567"


def test_registry_declares_all_six_channels():
    assert build_roskomnadzor_source_spec()["code"] == "roskomnadzor"
    specs = build_roskomnadzor_dataset_specs(1)
    assert len(specs) == 6
    assert {item["update_mode"] for item in specs} == {"bulk", "api"}


def test_template_exposes_states_but_not_private_payload():
    checks = {key: {"result": "unavailable", "reason": "dataset_not_loaded", "data_date": None} for key in ("communications", "broadcast", "media", "information_distributors", "hosting", "pd_operators")}
    html = Environment(loader=FileSystemLoader("templates")).get_template("partials/roskomnadzor.html").render(company={"inn": "7701234567", "roskomnadzor_checks": checks})
    assert "Лицензии связи" in html and "Операторы персональных данных" in html
    assert "Данные физлиц и ИП хранятся отдельно" in html
