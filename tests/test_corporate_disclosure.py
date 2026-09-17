from datetime import date

import pytest

from app.aggregators import company_product_aggregator
from app.providers.prime_disclosure_provider import PrimeDisclosureProviderError, parse_prime_disclosure_html


FOUND_HTML = """
<html><body><div id="ctl00_Main_SuccessLoad">
<span id="ctl00_Main_fullName"><b>АКЦИОНЕРНОЕ ОБЩЕСТВО &quot;ТЕСТ&quot;</b></span>
<span id="ctl00_Main_shortName">АО &quot;ТЕСТ&quot;</span>
<span id="ctl00_Main_legalAddress">Москва</span>
<span id="ctl00_Main_postAddress">Москва, а/я 1</span>
<span id="ctl00_Main_regDate">21.01.1994 00:00:00</span>
<span id="ctl00_Main_regAgency">Регистратор</span>
<span id="ctl00_Main_ogrn">1026301979527</span>
<span id="ctl00_Main_inn">6320004911</span>
<span id="ctl00_Main_fsfr">00001-A</span>
<span id="ctl00_Main_okpo">20976755</span>
<table><tr><td>1</td><td>Годовой отчет за 2025 г.</td><td>29.06.2026</td><td>29.06.2026</td>
<td><a href="/Portal/GetDocument.aspx?emId=6320004911&amp;docId=abc">Загрузить</a></td></tr></table>
</div></body></html>
"""


def test_parse_prime_exact_inn_profile_and_document_metadata():
    result = parse_prime_disclosure_html(FOUND_HTML, "6320004911")
    assert result["found"] is True
    assert result["profile"]["inn"] == "6320004911"
    assert result["profile"]["ogrn"] == "1026301979527"
    assert result["profile"]["registration_date"] == "1994-01-21"
    assert result["document_count"] == 1
    assert result["documents"][0]["title"] == "Годовой отчет за 2025 г."
    assert result["documents"][0]["publication_date"] == "2026-06-29"
    assert result["documents"][0]["source_url"].startswith("https://disclosure.1prime.ru/")


def test_parse_prime_explicit_empty_is_not_found():
    result = parse_prime_disclosure_html(
        '<div id="ctl00_Main_FailLoad"><p>Неверно указан ID эмитента или нет данных</p></div>',
        "7707329152",
    )
    assert result == {"found": False, "profile": None, "documents": [], "document_count": 0}


def test_parse_prime_mismatch_is_unavailable_not_not_found():
    with pytest.raises(PrimeDisclosureProviderError, match="exact ИНН") as error:
        parse_prime_disclosure_html(FOUND_HTML, "7707329152")
    assert error.value.kind == "invalid_response"


def test_parse_prime_requires_company_inn():
    with pytest.raises(ValueError, match="10-значному"):
        parse_prime_disclosure_html(FOUND_HTML, "123456789012")


def test_product_enrichment_marks_only_completed_disclosure(monkeypatch):
    company = {"inn": "6320004911", "sources_used": ["master_registry"]}
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_corporate_disclosure_check",
        lambda inn: {"result": "found", "profile": {"inn": inn}},
    )

    result = company_product_aggregator.enrich_company_with_corporate_disclosure(company)

    assert result["corporate_disclosure_check"]["profile"]["inn"] == "6320004911"
    assert result["sources_used"] == ["master_registry", "prime_disclosure"]
    assert company["sources_used"] == ["master_registry"]
