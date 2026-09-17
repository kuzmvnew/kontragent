from datetime import date

import httpx
import pytest

from app.providers.nostroy_provider import NOSTROY_API_URL, NostroyMemberProvider, NostroyProviderError, parse_member_search
from app.providers.nopriz_provider import NOPRIZ_API_URL, NoprizMemberProvider
from app.providers.sro_person_provider import NOPRIZ_NRS_API_URL, NoprizNrsProvider, NostroyNrsProvider, parse_nopriz_nrs_search
from app.services.nostroy_service import get_cached_nostroy_check
from app.models.nostroy import SroPersonRegistryRecord
from app.services.sro_registry_service import build_sro_dataset_specs
from jinja2 import Environment, FileSystemLoader


ROW = {
    "id": 4047586,
    "inn": "5907056036",
    "ogrnip": "1135907001801",
    "registration_number": "906",
    "inventory_number": "С-171-005907056036-0906",
    "registry_registration_date": "2014-03-12T00:00:00+04:00",
    "member_status": {"code": "2", "title": "Исключен"},
    "sro": {"id": 238, "registration_number": "СРО-С-171-13012010", "full_description": "СРО Строители Урала"},
    "last_updated_at_date_time_string": "01.08.2018 09:55:03",
    "director": "Персональные данные не должны выйти",
    "phones": "+7 secret",
}


def payload(rows):
    return {"data": {"data": rows, "page": 1, "countPages": 1.0 if rows else 0.0, "count": len(rows)}, "success": True, "message": ""}


def test_parser_accepts_exact_inn_and_excludes_person_fields():
    result = parse_member_search(payload([ROW]), "5907056036")
    assert result["found"] is True and result["total"] == 1
    assert result["records"][0]["ogrn"] == "1135907001801"
    assert result["records"][0]["member_status"] == "Исключен"
    assert "director" not in result["records"][0] and "phones" not in result["records"][0]


def test_parser_empty_is_not_found_but_mismatch_is_unavailable():
    assert parse_member_search(payload([]), "9102309919") == {"found": False, "records": [], "total": 0}
    with pytest.raises(NostroyProviderError) as error:
        parse_member_search(payload([ROW]), "9102309919")
    assert error.value.kind == "identity_mismatch"


def test_parser_rejects_inconsistent_count_and_bad_ogrn():
    broken = payload([ROW]); broken["data"]["count"] = 2
    with pytest.raises(NostroyProviderError, match="Счётчик"):
        parse_member_search(broken, "5907056036")
    bad = {**ROW, "ogrnip": "123"}
    with pytest.raises(NostroyProviderError) as error:
        parse_member_search(payload([bad]), "5907056036")
    assert error.value.kind == "identity_mismatch"


def test_provider_posts_one_low_load_exact_query():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload([ROW]))
    provider = NostroyMemberProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.check_inn("5907056036")
    assert result["found"] is True and len(seen) == 1
    assert str(seen[0].url) == NOSTROY_API_URL
    assert b'"searchString":"5907056036"' in seen[0].content


def test_provider_errors_never_become_not_found():
    provider = NostroyMemberProvider(client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429))))
    with pytest.raises(NostroyProviderError) as error:
        provider.check_inn("5907056036")
    assert error.value.kind == "source_protection"


def test_context_and_entity_type_determine_applicability_without_db():
    ip = get_cached_nostroy_check("123456789012", applicable=True)
    retailer = get_cached_nostroy_check("5907056036", applicable=False)
    unknown = get_cached_nostroy_check("5907056036", applicable=None)
    assert ip["result"] == retailer["result"] == "not_applicable"
    assert unknown["result"] == "unavailable" and unknown["reason"] == "applicability_not_determined"


def test_request_date_is_a_cache_date_not_registry_event_date(monkeypatch):
    class Session:
        def scalar(self, statement): return None
        def close(self): pass
    monkeypatch.setattr("app.services.nostroy_service.get_session", Session)
    result = get_cached_nostroy_check("5907056036", date(2026, 9, 17), applicable=True)
    assert result["result"] == "unavailable" and result["reason"] == "not_checked"
    assert result["request_date"] == date(2026, 9, 17)


def test_nopriz_provider_uses_separate_official_endpoint_and_company_safe_parser():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload([ROW]))
    result = NoprizMemberProvider(client=httpx.Client(transport=httpx.MockTransport(handler))).check_inn("5907056036")
    assert result["found"] is True and str(seen[0].url) == NOPRIZ_API_URL
    assert "director" not in result["records"][0]


NRS_ROW = {
    "id": 11,
    "registrationNumber": "П-000011",
    "fio": "Private Specialist",
    "inclusionProtocolDate": "23.05.2017 *",
    "changesDate": "27.05.2025",
    "workTypes": {"project": {"statusCode": "inactive", "statusTitle": "Исключен", "exclusionDate": "27.05.2025"}},
}


def nrs_payload(rows):
    return {"success": True, "message": "OK", "data": {"data": rows, "count": len(rows), "countPages": 1 if rows else 0, "page": 1, "pageCount": "20"}}


def test_nopriz_nrs_requires_exact_official_id_and_builds_private_minimum():
    parsed = parse_nopriz_nrs_search(nrs_payload([NRS_ROW]), "П-000011")
    assert parsed["found"] is True
    record = parsed["records"][0]
    assert record["source_record_id"] == "11" and record["registration_number"] == "П-000011"
    assert record["person_name"] == "Private Specialist"
    assert "phone" not in record and "email" not in record and "address" not in record


def test_nopriz_nrs_provider_is_one_low_load_exact_record_request():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=nrs_payload([NRS_ROW]))
    result = NoprizNrsProvider(client=httpx.Client(transport=httpx.MockTransport(handler))).check_registration_number("П-000011")
    assert result["total"] == 1 and len(seen) == 1 and str(seen[0].url) == NOPRIZ_NRS_API_URL
    assert '"searchString":"П-000011"'.encode() in seen[0].content


def test_nostroy_nrs_protected_images_are_unavailable_not_not_found():
    with pytest.raises(NostroyProviderError) as error:
        NostroyNrsProvider().check_registration_number("С-000001")
    assert error.value.kind == "source_protection"


def test_private_person_model_is_non_public_by_default_and_not_in_template():
    assert SroPersonRegistryRecord.public_visibility.default.arg is False
    check = {"result": "found", "record_count": 1, "active_record_count": 1, "records": [{"member_status": "Является членом", "inventory_number": "X", "registration_number": "1", "sro_registration_number": "SRO"}], "data_date": date(2026, 9, 17), "interpretation_note": "note", "coverage_note": "coverage", "reason": None}
    html = Environment(loader=FileSystemLoader("templates")).get_template("partials/sro.html").render(company={"inn": "5907056036", "sro_checks": {"nostroy": check, "nopriz": check}, "sro_person_records": [{"person_name": "Private Specialist"}]})
    assert "Private Specialist" not in html
    assert "private-контуре" in html and "exact ИНН" in html


def test_registry_specs_separate_company_and_private_datasets():
    specs = build_sro_dataset_specs({"nostroy": 1, "nopriz": 2})
    assert {item["code"] for item in specs} == {"nostroy_sro_members_on_demand", "nopriz_sro_members_on_demand", "nopriz_nrs_private_on_demand", "nostroy_nrs_protected"}
    private = [item for item in specs if "nrs" in item["code"]]
    assert all(item["domain"] == "professional_registry_private" for item in private)


def test_dated_error_cache_is_the_source_backoff(monkeypatch):
    import app.services.nopriz_service as service

    cached = {"result": "unavailable", "reason": "timeout"}
    monkeypatch.setattr(service, "ensure_sro_datasets", lambda: None)
    monkeypatch.setattr(service, "get_cached_nopriz_check", lambda *args, **kwargs: cached)

    class MustNotRun:
        def check_inn(self, _inn):
            raise AssertionError("dated source error must be reused")

    monkeypatch.setattr(service, "NoprizMemberProvider", MustNotRun)
    assert service.refresh_nopriz_check("7810978295") is cached
