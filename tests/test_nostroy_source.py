from datetime import date

import httpx
import pytest

from app.providers.nostroy_provider import NOSTROY_API_URL, NostroyMemberProvider, NostroyProviderError, parse_member_search
from app.services.nostroy_service import get_cached_nostroy_check


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
