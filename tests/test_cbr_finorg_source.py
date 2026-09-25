from datetime import date, datetime, timezone
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.aggregators import company_product_aggregator
from app.providers.cbr_finorg_provider import (
    CbrFinorgProvider,
    SERVICE_URL,
    parse_full_info_response,
    parse_search_by_inns_response,
)
from app.services import cbr_finorg_service
from app.services.cbr_finorg_registry_service import (
    build_cbr_finorg_dataset_spec,
    build_cbr_finorg_source_spec,
)


SEARCH_FOUND_XML = '''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <SearchByINNsResponse xmlns="http://web.cbr.ru/">
      <SearchByINNsResult>
        <DS>
          <Record>
            <Id>101</Id>
            <OGRN>1027700132195</OGRN>
            <INN>7707083893</INN>
            <Name>ПАО ТЕСТ БАНК</Name>
            <Status>Active</Status>
            <ErrorText></ErrorText>
          </Record>
        </DS>
        <Error></Error>
        <IsSucess>true</IsSucess>
      </SearchByINNsResult>
    </SearchByINNsResponse>
  </soap:Body>
</soap:Envelope>'''.encode("utf-8")

SEARCH_NOT_FOUND_XML = '''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <SearchByINNsResponse xmlns="http://web.cbr.ru/">
      <SearchByINNsResult>
        <DS />
        <Error></Error>
        <IsSucess>true</IsSucess>
      </SearchByINNsResult>
    </SearchByINNsResponse>
  </soap:Body>
</soap:Envelope>'''.encode("utf-8")

FULL_INFO_XML = '''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetFullInfoByINNResponse xmlns="http://web.cbr.ru/">
      <GetFullInfoByINNResult>
        <ID>101</ID>
        <OGRN>1027700132195</OGRN>
        <INN>7707083893</INN>
        <ShortName>ПАО ТЕСТ БАНК</ShortName>
        <Name>Публичное акционерное общество ТЕСТ БАНК</Name>
        <Address>Москва</Address>
        <Phones>+7 495 000-00-00</Phones>
        <Email>info@example.test</Email>
        <OKATO>45</OKATO>
        <Reg>Москва</Reg>
        <FOTypes>
          <string>Кредитные организации</string>
        </FOTypes>
        <Status>Active</Status>
        <IsSroMember>false</IsSroMember>
        <REGNUM>1481</REGNUM>
        <BIC>044525225</BIC>
        <LicList>
          <LicInfo>
            <VidID>1</VidID>
            <VidD>Банковские операции</VidD>
            <LIC_Number>1481</LIC_Number>
            <LIC_Name>Универсальная лицензия</LIC_Name>
            <LIC_DTStart>2015-08-11T00:00:00</LIC_DTStart>
            <LIC_DTEnd>0001-01-01T00:00:00</LIC_DTEnd>
          </LicInfo>
        </LicList>
        <PaymentSystems />
        <WebSites>
          <string>https://example.test</string>
        </WebSites>
        <MFOList />
        <HasBranches>true</HasBranches>
        <Error></Error>
        <BnkStatus>Действует</BnkStatus>
        <RegistrationDate>1991-06-20T00:00:00</RegistrationDate>
      </GetFullInfoByINNResult>
    </GetFullInfoByINNResponse>
  </soap:Body>
</soap:Envelope>'''.encode("utf-8")


class FakeResponse:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, content, headers):
        self.calls.append(
            {
                "url": url,
                "content": content,
                "headers": headers,
            }
        )
        return self.responses.pop(0)


class FoundProvider:
    def check_inn(self, inn):
        return {
            "found": True,
            "participant": {
                "cbr_id": 101,
                "ogrn": "1027700132195",
                "inn": inn,
                "short_name": "ПАО ТЕСТ БАНК",
                "name": "ПАО ТЕСТ БАНК",
                "status": "Active",
                "fo_types": ["Кредитные организации"],
                "licenses": [
                    {
                        "activity_id": 1,
                        "activity": "Банковские операции",
                        "number": "1481",
                        "name": "Универсальная лицензия",
                        "start_date": "2015-08-11",
                        "end_date": None,
                    }
                ],
                "payment_systems": [],
                "mfo_history": [],
                "websites": [],
                "address": "Москва",
                "phones": None,
                "email": None,
                "region": "Москва",
                "regnum": "1481",
                "bic": "044525225",
                "is_sro_member": False,
                "has_branches": True,
                "registration_date": "1991-06-20",
            },
            "search_record": {
                "inn": inn,
            },
            "http_status": 200,
        }


def make_saved_row():
    return SimpleNamespace(
        id=1,
        inn="7707083893",
        request_date=date(2026, 9, 16),
        result_status="success",
        is_participant=True,
        cbr_id=101,
        ogrn="1027700132195",
        short_name="ПАО ТЕСТ БАНК",
        name="ПАО ТЕСТ БАНК",
        status="Active",
        fo_types=["Кредитные организации"],
        licenses=[
            {
                "activity_id": 1,
                "activity": "Банковские операции",
                "number": "1481",
                "name": "Универсальная лицензия",
                "start_date": "2015-08-11",
                "end_date": None,
            }
        ],
        payment_systems=[],
        mfo_history=[],
        regnum="1481",
        bic="044525225",
        registration_date=date(1991, 6, 20),
        checked_at=datetime(
            2026,
            9,
            16,
            10,
            0,
            tzinfo=timezone.utc,
        ),
        error_code=None,
        error_message=None,
        http_status=200,
    )


def test_search_parser_matches_only_exact_inn():
    result = parse_search_by_inns_response(
        SEARCH_FOUND_XML,
        requested_inn="7707083893",
    )

    assert result["found"] is True
    assert result["record"]["cbr_id"] == 101
    assert result["record"]["inn"] == "7707083893"


def test_full_info_parser_reads_licenses_and_status():
    result = parse_full_info_response(
        FULL_INFO_XML,
        requested_inn="7707083893",
    )

    assert result["inn"] == "7707083893"
    assert result["status"] == "Active"
    assert result["fo_types"] == [
        "Кредитные организации"
    ]
    assert result["licenses"][0]["number"] == "1481"
    assert result["licenses"][0]["end_date"] is None
    assert result["registration_date"] == "1991-06-20"


def test_provider_uses_search_then_full_info():
    client = FakeClient(
        [
            FakeResponse(SEARCH_FOUND_XML),
            FakeResponse(FULL_INFO_XML),
        ]
    )
    provider = CbrFinorgProvider(client=client)

    result = provider.check_inn("7707083893")

    assert result["found"] is True
    assert result["participant"]["cbr_id"] == 101
    assert len(client.calls) == 2
    assert all(
        call["url"] == SERVICE_URL
        for call in client.calls
    )
    assert "SearchByINNs" in client.calls[0][
        "headers"
    ]["SOAPAction"]
    assert "GetFullInfoByINN" in client.calls[1][
        "headers"
    ]["SOAPAction"]


def test_provider_not_found_uses_only_search_call():
    client = FakeClient(
        [FakeResponse(SEARCH_NOT_FOUND_XML)]
    )
    provider = CbrFinorgProvider(client=client)

    result = provider.check_inn("7701234567")

    assert result["found"] is False
    assert result["participant"] is None
    assert len(client.calls) == 1


def test_provider_accepts_individual_entrepreneur_inn():
    client = FakeClient(
        [FakeResponse(SEARCH_NOT_FOUND_XML)]
    )
    provider = CbrFinorgProvider(client=client)

    result = provider.check_inn("770123456789")

    assert result["found"] is False
    assert len(client.calls) == 1
    assert b"770123456789" in client.calls[0]["content"]


def test_service_refresh_returns_found(monkeypatch):
    row = make_saved_row()

    monkeypatch.setattr(
        cbr_finorg_service,
        "ensure_cbr_finorg_dataset",
        lambda: None,
    )
    monkeypatch.setattr(
        cbr_finorg_service,
        "save_cbr_finorg_attempt",
        lambda **kwargs: row,
    )

    result = (
        cbr_finorg_service
        .refresh_cbr_finorg_check_for_inn(
            "7707083893",
            request_date=date(2026, 9, 16),
            provider=FoundProvider(),
        )
    )

    assert result["result"] == "found"
    assert result["is_participant"] is True
    assert result["active_license_count"] == 1
    assert result["cached"] is False


def test_product_aggregator_adds_cbr_finorg_source(monkeypatch):
    company = {
        "inn": "7707083893",
        "sources_used": ["fns"],
    }
    check = {
        "result": "found",
        "dataset_code": "cbr_finorg",
        "source": "cbr_finorg",
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_cbr_finorg_check_for_inn",
        lambda inn: check,
    )

    result = (
        company_product_aggregator
        .enrich_company_with_cbr_finorg(company)
    )

    assert result["cbr_finorg_check"] == check
    assert "cbr_finorg" in result["sources_used"]
    assert company["sources_used"] == ["fns"]


def test_registry_specs_are_official_scheduled_and_on_demand_api():
    source = build_cbr_finorg_source_spec()
    dataset = build_cbr_finorg_dataset_spec(
        source_id=10
    )

    assert source["source_type"] == "official"
    assert dataset["code"] == "cbr_finorg"
    assert dataset["update_mode"] == "api"
    assert dataset["data_format"] == "soap_xml"
    assert dataset["refresh_schedule"] == "daily"
    assert dataset["dataset_kind"] == "scheduled_and_on_demand_api"
    assert dataset["freshness_policy"] == "daily"


def test_cbr_finorg_template_compiles():
    env = Environment(
        loader=FileSystemLoader("templates"),
    )

    template = env.get_template(
        "partials/cbr_finorg.html"
    )

    assert template is not None
