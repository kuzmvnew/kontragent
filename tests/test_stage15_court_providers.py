from datetime import date

import pytest

from app.providers.arbitration_court_provider import ArbitrationCourtProviderError, CheckoArbitrationProvider, parse_checko_legal_cases
from app.providers.general_court_provider import GeneralCourtRouter, MoscowCourtProvider, RegionalSudrfProvider, parse_moscow_court_search_html
from app.services.arbitration_court_service import calculate_arbitration_signals


MOSCOW_HTML = """
<table><tbody><tr data-href="/rs/test/services/cases/civil/details/uuid?participant=x">
<td>02-1183/2026 Скопировать в буфер обмена</td>
<td><p>Истец</p>Иванов И.И.<br><p>Ответчик</p>АКЦИОНЕРНОЕ ОБЩЕСТВО «АВТОВАЗ»</td>
<td>Обжаловано, 21.05.2026</td>
<td>179 - О защите прав потребителей</td>
<td>Судья</td>
<td>Гражданские дела (Гагаринский районный суд)</td>
<td>Отказано в удовлетворении, 19.03.2026</td><td></td>
</tr></tbody></table>
"""


def test_moscow_parser_keeps_official_evidence_and_medium_name_confidence():
    result = parse_moscow_court_search_html(MOSCOW_HTML, full_name='АКЦИОНЕРНОЕ ОБЩЕСТВО "АВТОВАЗ"')
    assert result["result_count"] == 1
    case = result["cases"][0]
    assert case["case_number"] == "02-1183/2026"
    assert case["role"] == "defendant"
    assert case["event_date"] == "2026-05-21"
    assert case["result_date"] == "2026-03-19"
    assert case["matching_method"] == "exact_full_legal_name"
    assert case["confidence"] == "medium"
    assert case["source_url"].startswith("https://mos-gorsud.ru/")
    assert result["coverage"]["coverage_label"] == "MOSCOW TARGETED QUERY (FIRST 100) / OTHER REGIONS NOT CHECKED"


def test_moscow_parser_rejects_name_only_non_exact_discovery():
    result = parse_moscow_court_search_html(MOSCOW_HTML, full_name='АКЦИОНЕРНОЕ ОБЩЕСТВО "ДРУГАЯ КОМПАНИЯ"')
    assert result["cases"] == []


def test_regional_sudrf_uses_shared_exact_identifier_contract():
    params = RegionalSudrfProvider.build_exact_identifier_params(inn="6320002223", ogrn="1026301983113", delo_id="1540005")
    assert params["G2_PARTS__INN_STRSS"] == "6320002223"
    assert params["G2_PARTS__OGRN_STRSS"] == "1026301983113"
    assert params["name_op"] == "sf"
    assert params["new"] == "5"


def test_checko_parser_preserves_loaded_sample_vs_full_period():
    payload = {
        "ЗапВсего": 250,
        "СтрВсего": 3,
        "СтрТекущ": 1,
        "Записи": [{"Номер": "А40-1/2026", "Суд": "АС г. Москвы", "Истцы": [], "Ответчики": [], "СуммаИска": 123}],
    }
    result = parse_checko_legal_cases(payload, page=1, limit=100)
    assert result["loaded_count"] == 1
    assert result["total_count"] == 250
    assert result["total_pages"] == 3
    assert result["is_full_period_loaded"] is False
    assert result["cases"][0]["matching_method"] == "inn_exact"


def test_checko_requires_key_without_making_request():
    class NeverCalled:
        def get(self, *args, **kwargs):
            raise AssertionError("external request must not occur without key")

    provider = CheckoArbitrationProvider(api_key=None, client=NeverCalled())
    provider.api_key = None
    with pytest.raises(ArbitrationCourtProviderError) as error:
        provider.search_company(inn="6320002223", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), page=1)
    assert error.value.kind == "access_pending"


def test_checko_one_click_is_exactly_one_page_of_100():
    class Response:
        status_code = 200
        url = "https://api.checko.ru/v2/legal-cases?page=2"
        def json(self): return {"ЗапВсего": 201, "СтрВсего": 3, "СтрТекущ": 2, "Записи": []}

    class Client:
        def __init__(self): self.calls = []
        def get(self, url, params): self.calls.append((url, params)); return Response()

    client = Client()
    provider = CheckoArbitrationProvider(api_key="free-key", client=client)
    result = provider.search_company(inn="6320002223", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), page=2)
    assert len(client.calls) == 1
    assert client.calls[0][1]["limit"] == 100
    assert client.calls[0][1]["page"] == 2
    assert result["current_page"] == 2


def test_moscow_provider_makes_one_targeted_first_page_request():
    class Response:
        status_code = 200
        text = MOSCOW_HTML

    class Client:
        def __init__(self): self.calls = []
        def get(self, url, params): self.calls.append((url, params)); return Response()

    client = Client()
    provider = MoscowCourtProvider(client=client)
    provider.search_company(inn="6320002223", ogrn="1026301983113", full_name='АКЦИОНЕРНОЕ ОБЩЕСТВО "АВТОВАЗ"')
    assert len(client.calls) == 1
    assert client.calls[0][1] == {"participant": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "АВТОВАЗ"', "limit": 100, "page": 1}


@pytest.mark.parametrize("region_code,expected", [("77", "moscow_courts_official"), ("78", "regional_sudrf_official"), ("66", "regional_sudrf_official")])
def test_general_court_router_selects_one_targeted_region(region_code, expected):
    route, provider = GeneralCourtRouter().route(region_code)
    assert route.provider_code == expected
    assert provider.code == expected


def test_spb_and_sverdlovsk_routes_keep_exact_identifier_contract():
    router = GeneralCourtRouter()
    for region_code in ("78", "66"):
        route, provider = router.route(region_code)
        url = provider.build_search_url(inn="6320002223", ogrn="1026301983113")
        assert route.portal_url in url
        assert "G2_PARTS__INN_STRSS=6320002223" in url
        assert "G2_PARTS__OGRN_STRSS=1026301983113" in url


def test_arbitration_signals_are_period_and_coverage_aware():
    cases = [{"filing_date": "2026-09-10", "claim_amount": "100.50", "claimants": [{"ИНН": "1215214540"}], "defendants": []}]
    signals = calculate_arbitration_signals(cases=cases, inn="1215214540", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), total_count=54, full_period_loaded=False)
    assert signals["total_case_count"]["numerator"] == 1
    assert signals["total_case_count"]["coverage"] == "loaded_sample"
    assert signals["claimant_count"]["numerator"] == 1
    assert signals["total_claim_amount"]["numerator"] == "100.50"
    assert signals["new_cases_30d"]["numerator"] == 1
    assert signals["active_count"]["coverage"] == "unavailable_not_provable"
    assert signals["claims_to_revenue"]["coverage"] == "unavailable_revenue"
