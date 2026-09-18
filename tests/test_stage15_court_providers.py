from datetime import date

import pytest

from app.providers.arbitration_court_provider import ArbitrationCourtProviderError, CheckoArbitrationProvider, parse_checko_legal_cases
from app.providers.general_court_provider import GeneralCourtRouter, MoscowCourtProvider, RegionalSudrfProvider, parse_moscow_court_search_html
from app.services import arbitration_court_service
from app.services import general_court_service
from app.services.arbitration_court_service import (
    _serialize,
    calculate_arbitration_signals,
    get_cached_arbitration_court_check,
)


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
        "Записи": [{"Номер": "А40-1/2026", "Суд": "АС г. Москвы", "СтрКАД": "https://kad.arbitr.ru/Card/test", "Ист": [{"ИНН": "1215214540"}], "Ответ": [], "СуммИск": 123}],
    }
    result = parse_checko_legal_cases(payload, page=1, limit=100)
    assert result["loaded_count"] == 1
    assert result["total_count"] == 250
    assert result["total_pages"] == 3
    assert result["is_full_period_loaded"] is False
    assert result["cases"][0]["matching_method"] == "inn_exact"
    assert result["cases"][0]["source_url"] == "https://kad.arbitr.ru/Card/test"
    assert result["cases"][0]["claimants"][0]["ИНН"] == "1215214540"
    assert result["cases"][0]["claim_amount"] == 123


def test_checko_requires_key_without_making_request():
    class NeverCalled:
        def post(self, *args, **kwargs):
            raise AssertionError("external request must not occur without key")

    provider = CheckoArbitrationProvider(api_key=None, client=NeverCalled())
    provider.api_key = None
    with pytest.raises(ArbitrationCourtProviderError) as error:
        provider.search_company(inn="6320002223", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), page=1)
    assert error.value.kind == "access_pending"


def test_checko_one_click_is_exactly_one_page_of_100():
    class Response:
        status_code = 200
        url = "https://api.checko.ru/v2/legal-cases"
        def json(self): return {"ЗапВсего": 201, "СтрВсего": 3, "СтрТекущ": 2, "Записи": []}

    class Client:
        def __init__(self): self.calls = []
        def post(self, url, json): self.calls.append((url, json)); return Response()

    client = Client()
    provider = CheckoArbitrationProvider(api_key="free-key", client=client)
    result = provider.search_company(inn="6320002223", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), page=2)
    assert len(client.calls) == 1
    assert client.calls[0][1]["limit"] == 100
    assert client.calls[0][1]["page"] == 2
    assert client.calls[0][1]["sort"] == "-date"
    assert result["current_page"] == 2
    assert result["source_url"] == "https://api.checko.ru/v2/legal-cases"


def test_checko_exhausted_local_quota_is_unavailable_without_request():
    class NeverCalled:
        def post(self, *args, **kwargs):
            raise AssertionError("external request must not occur after local quota exhaustion")

    provider = CheckoArbitrationProvider(api_key="free-key", client=NeverCalled(), quota_guard=lambda: False)
    with pytest.raises(ArbitrationCourtProviderError) as error:
        provider.search_company(inn="1215214540", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), page=1)
    assert error.value.kind == "quota_exhausted"


def test_checko_http_200_api_error_is_not_false_not_found():
    class Response:
        status_code = 200
        url = "https://api.checko.ru/v2/legal-cases"

        def json(self):
            return {"meta": {"status": "error", "message": "Дневной лимит запросов исчерпан"}}

    class Client:
        def post(self, *args, **kwargs):
            return Response()

    provider = CheckoArbitrationProvider(api_key="free-key", client=Client())
    with pytest.raises(ArbitrationCourtProviderError) as error:
        provider.search_company(inn="1215214540", date_from=date(2025, 9, 17), date_to=date(2026, 9, 17), page=1)
    assert error.value.kind == "quota_exhausted"


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
    assert signals["reported_total_cases"]["numerator"] == 54
    assert signals["total_case_count"]["coverage"] == "loaded_sample"
    assert signals["claimant_count"]["numerator"] == 1
    assert signals["total_claim_amount"]["numerator"] == "100.50"
    assert signals["new_cases_30d"]["numerator"] == 1
    assert signals["active_count"]["coverage"] == "unavailable_not_provable"
    assert signals["claims_to_revenue"]["coverage"] == "unavailable_revenue"


def test_arbitration_result_semantics_keep_not_found_and_unavailable_distinct():
    class Row:
        result_status = "success"
        date_to = date(2026, 9, 17)
        date_from = date(2025, 9, 17)
        cases = []
        loaded_pages = 1
        total_pages = 1
        total_count = 0
        inn = "1215214540"
        source_url = "https://api.checko.ru/v2/legal-cases"
        checked_at = None
        error_code = None
        error_message = None

    assert _serialize(Row())["result"] == "not_found"
    Row.result_status = "error"
    Row.error_code = "quota_exhausted"
    result = _serialize(Row())
    assert result["result"] == "unavailable"
    assert result["reason"] == "quota_exhausted"


def test_recent_successful_arbitration_snapshot_is_reused_as_partial(monkeypatch):
    class Row:
        result_status = "success"
        date_to = date(2026, 9, 17)
        date_from = date(2025, 9, 17)
        cases = [{"filing_date": "2026-09-10", "claimants": [], "defendants": []}]
        loaded_pages = 1
        total_pages = 1
        total_count = 1
        inn = "1215214540"
        source_url = "https://api.checko.ru/v2/legal-cases"
        checked_at = None
        error_code = None
        error_message = None

    class Session:
        def scalar(self, _statement): return Row()
        def close(self): pass

    session = Session()
    monkeypatch.setattr(arbitration_court_service, "get_session", lambda: session)
    result = get_cached_arbitration_court_check(
        "1215214540", request_date=date(2026, 9, 18),
    )
    assert result["result"] == "found"
    assert result["coverage_complete"] is False
    assert 0.99 < result["normalized_coverage"] < 1
    assert result["stored_period"]["to"] == "2026-09-17"
    assert result["requested_period"]["to"] == "2026-09-18"
    assert "bridge-snapshot" in result["coverage_note"]


def test_explicit_regional_provider_error_persists_its_own_source_url(monkeypatch):
    class Company:
        id = 1
        inn = "6320002223"
        ogrn = "1026301983113"
        name = "АО ТЕСТ"
        full_name = "АКЦИОНЕРНОЕ ОБЩЕСТВО ТЕСТ"

    class Provider:
        code = "regional_sudrf_official"
        base_url = "https://oblsud--svd.sudrf.ru/modules.php"
        def search_company(self, **_kwargs):
            raise general_court_service.GeneralCourtProviderError(
                kind="timeout", message="timeout",
            )

    class ResultRow:
        result_status = "error"
        provider_code = Provider.code
        request_date = date(2026, 9, 18)
        cases = []
        coverage = {"coverage_label": "CHECK FAILED; NO NEGATIVE INFERENCE"}
        source_url = Provider.base_url
        error_code = "timeout"
        error_message = "timeout"
        checked_at = None

    class Session:
        def __init__(self, scalar_value): self.scalar_value = scalar_value
        def scalar(self, _statement): return self.scalar_value
        def execute(self, statement):
            values = statement.compile().params
            assert values["source_url"] == Provider.base_url
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    sessions = iter((Session(Company()), Session(ResultRow())))
    monkeypatch.setattr(general_court_service, "ensure_stage15_dataset", lambda _code: 1)
    monkeypatch.setattr(general_court_service, "get_session", lambda: next(sessions))
    result = general_court_service.refresh_general_court_check(
        "6320002223", request_date=date(2026, 9, 18),
        provider=Provider(), force_refresh=True,
    )
    assert result["source_url"] == Provider.base_url
    assert result["source"] == Provider.code
    assert result["result"] == "unavailable"
