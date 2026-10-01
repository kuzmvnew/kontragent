import base64
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy.dialects import postgresql

from app.ingestion import exact_source_workers as worker
from app.providers.nopriz_provider import (
    NOPRIZ_API_URL,
    NoprizCandidatePage,
    NoprizMemberProvider,
    parse_nopriz_candidate_pages,
)
from app.providers.nostroy_provider import parse_member_search
from app.services import nopriz_service
from app.worker.contracts import HandlerContext


REQUESTED_INN = "0100003333"
EXACT_INN = "7701034434"
NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


def _row(member_id, inn, *, ogrn="1027700132195", registration_number=None):
    return {
        "id": member_id,
        "inn": inn,
        "ogrnip": ogrn,
        "registration_number": registration_number or str(member_id),
        "inventory_number": f"INV-{member_id}",
        "registry_registration_date": "2020-01-02T00:00:00+03:00",
        "member_status": {"code": "1", "title": "Является членом"},
        "sro": {
            "id": 7,
            "registration_number": "СРО-П-007",
            "full_description": "Тестовая СРО",
        },
        "last_updated_at_date_time_string": "30.09.2026 10:00:00",
        "director": "protected",
        "phones": "+7 protected",
    }


def _payload(rows, *, page=1, count=None, count_pages=None, page_size=100):
    count = len(rows) if count is None else count
    if count_pages is None:
        count_pages = (count + page_size - 1) // page_size if count else 0
    return {
        "success": True,
        "message": "",
        "data": {
            "data": rows,
            "page": page,
            "countPages": count_pages,
            "count": count,
        },
    }


def _candidate(payload, *, requested_page=1):
    content = json.dumps(payload, ensure_ascii=False).encode()
    request = {
        "filters": {"inn": REQUESTED_INN},
        "searchString": "",
        "page": requested_page,
        "pageCount": 100,
        "sortBy": {},
    }
    return NoprizCandidatePage(
        requested_page=requested_page,
        request=request,
        payload=payload,
        response_content=content,
        response_headers={"content-type": "application/json", "x-secret": "no"},
        http_status=200,
        retrieved_at=NOW,
    )


def test_real_shape_foreign_ogrnip_substring_is_unknown_and_not_public():
    foreign_ip = _row(
        101,
        "271304284596",
        ogrn="301000033330001",
    )

    result = parse_nopriz_candidate_pages(
        (_candidate(_payload([foreign_ip])),), REQUESTED_INN
    )

    assert result == {
        "result_status": "unknown",
        "is_found": None,
        "public_records": [],
        "record_count": None,
        "error_code": "exact_identity_not_confirmed",
        "found": None,
        "records": [],
        "total": None,
    }


def test_exact_company_row_is_safe_found_and_excludes_protected_fields():
    result = parse_nopriz_candidate_pages(
        (_candidate(_payload([_row(201, EXACT_INN)])),), EXACT_INN
    )

    assert result["result_status"] == "success"
    assert result["is_found"] is True
    assert result["record_count"] == 1
    assert result["public_records"][0]["inn"] == EXACT_INN
    assert "director" not in result["public_records"][0]
    assert "phones" not in result["public_records"][0]


def test_mixed_candidates_retain_only_full_string_exact_identity():
    rows = [
        _row(301, "271304284596", ogrn="301000033330001"),
        _row(302, EXACT_INN),
    ]

    result = parse_nopriz_candidate_pages(
        (_candidate(_payload(rows)),), EXACT_INN
    )

    assert result["record_count"] == 1
    assert [record["member_id"] for record in result["public_records"]] == [302]


def test_twelve_digit_ip_inn_is_never_truncated_to_company_inn():
    ip_row = _row(401, "770103443412", ogrn="315774600000001")

    result = parse_nopriz_candidate_pages(
        (_candidate(_payload([ip_row])),), EXACT_INN
    )

    assert result["result_status"] == "unknown"
    assert result["is_found"] is None
    assert result["public_records"] == []


def test_exact_company_identity_requires_legal_entity_ogrn_format():
    exact_with_ogrnip = _row(402, EXACT_INN, ogrn="315774600000001")

    result = parse_nopriz_candidate_pages(
        (_candidate(_payload([exact_with_ogrnip])),), EXACT_INN
    )

    assert result["result_status"] == "unknown"
    assert result["error_code"] == "exact_identity_invalid"
    assert result["public_records"] == []


def test_empty_candidate_result_is_unknown_never_not_found():
    result = parse_nopriz_candidate_pages(
        (_candidate(_payload([])),), EXACT_INN
    )

    assert result["result_status"] == "unknown"
    assert result["error_code"] == "exact_identity_not_confirmed"
    assert result["found"] is None
    assert result["record_count"] is None


def test_multiple_exact_memberships_are_preserved():
    rows = [
        _row(501, EXACT_INN, registration_number="A"),
        _row(502, EXACT_INN, registration_number="B"),
    ]

    result = parse_nopriz_candidate_pages(
        (_candidate(_payload(rows)),), EXACT_INN
    )

    assert result["record_count"] == 2
    assert {record["registration_number"] for record in result["public_records"]} == {"A", "B"}


def test_valid_two_page_candidate_traversal():
    first = _candidate(
        _payload(
            [_row(601, "271304284596"), _row(602, EXACT_INN)],
            page=1,
            count=3,
            count_pages=2,
            page_size=2,
        )
    )
    second = _candidate(
        _payload(
            [_row(603, EXACT_INN)],
            page=2,
            count=3,
            count_pages=2,
            page_size=2,
        ),
        requested_page=2,
    )

    result = parse_nopriz_candidate_pages(
        (first, second), EXACT_INN, page_size=2, max_pages=2
    )

    assert result["result_status"] == "success"
    assert [record["member_id"] for record in result["public_records"]] == [602, 603]


def test_count_drift_makes_result_unknown():
    first = _candidate(
        _payload([_row(701, EXACT_INN), _row(702, EXACT_INN)], page=1, count=3, count_pages=2, page_size=2)
    )
    second = _candidate(
        _payload([_row(703, EXACT_INN)], page=2, count=4, count_pages=2, page_size=2),
        requested_page=2,
    )

    result = parse_nopriz_candidate_pages(
        (first, second), EXACT_INN, page_size=2, max_pages=2
    )

    assert result["result_status"] == "unknown"
    assert result["error_code"] == "pagination_count_changed"
    assert result["public_records"] == []


def test_duplicate_member_across_pages_makes_result_unknown():
    first = _candidate(
        _payload([_row(801, EXACT_INN), _row(802, EXACT_INN)], page=1, count=3, count_pages=2, page_size=2)
    )
    second = _candidate(
        _payload([_row(802, EXACT_INN)], page=2, count=3, count_pages=2, page_size=2),
        requested_page=2,
    )

    result = parse_nopriz_candidate_pages(
        (first, second), EXACT_INN, page_size=2, max_pages=2
    )

    assert result["result_status"] == "unknown"
    assert result["error_code"] == "pagination_duplicate_member"


def test_duplicate_or_missing_page_never_produces_a_negative_fact():
    first = _candidate(
        _payload([_row(811, EXACT_INN), _row(812, EXACT_INN)], page=1, count=3, count_pages=2, page_size=2)
    )
    duplicate_page = _candidate(
        _payload([_row(813, EXACT_INN)], page=1, count=3, count_pages=2, page_size=2),
        requested_page=2,
    )

    duplicate_result = parse_nopriz_candidate_pages(
        (first, duplicate_page), EXACT_INN, page_size=2, max_pages=2
    )
    incomplete_result = parse_nopriz_candidate_pages(
        (first,), EXACT_INN, page_size=2, max_pages=2
    )

    assert duplicate_result["error_code"] == "pagination_page_mismatch"
    assert incomplete_result["error_code"] == "pagination_incomplete"
    assert duplicate_result["is_found"] is incomplete_result["is_found"] is None
    assert duplicate_result["public_records"] == incomplete_result["public_records"] == []


def test_count_pages_or_success_schema_change_is_unknown():
    first = _candidate(
        _payload([_row(821, EXACT_INN), _row(822, EXACT_INN)], page=1, count=3, count_pages=2, page_size=2)
    )
    changed = _candidate(
        _payload([_row(823, EXACT_INN)], page=2, count=3, count_pages=3, page_size=2),
        requested_page=2,
    )
    failed_payload = _payload([])
    failed_payload["success"] = False

    changed_result = parse_nopriz_candidate_pages(
        (first, changed), EXACT_INN, page_size=2, max_pages=3
    )
    schema_result = parse_nopriz_candidate_pages(
        (_candidate(failed_payload),), EXACT_INN
    )

    assert changed_result["error_code"] == "pagination_count_pages_changed"
    assert schema_result["error_code"] == "pagination_schema_inconsistent"
    assert changed_result["record_count"] is schema_result["record_count"] is None


def test_pagination_over_safety_bound_is_unknown_and_stops_after_first_page():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_payload(
                [_row(901, EXACT_INN), _row(902, EXACT_INN)],
                page=1,
                count=4,
                count_pages=2,
                page_size=2,
            ),
        )

    provider = NoprizMemberProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        page_size=2,
        max_pages=1,
    )
    result = provider.check_inn(EXACT_INN)

    assert len(seen) == 1
    assert result["result_status"] == "unknown"
    assert result["error_code"] == "pagination_limit_exceeded"
    assert result["public_records"] == []


def test_provider_uses_candidate_filter_and_validates_all_pages():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["page"] == 1:
            rows = [_row(1001, "271304284596"), _row(1002, EXACT_INN)]
        else:
            rows = [_row(1003, EXACT_INN)]
        return httpx.Response(
            200,
            json=_payload(rows, page=body["page"], count=3, count_pages=2, page_size=2),
        )

    provider = NoprizMemberProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        page_size=2,
        max_pages=2,
    )
    result = provider.check_inn(EXACT_INN)

    assert [request["page"] for request in requests] == [1, 2]
    assert all(request["filters"] == {"inn": EXACT_INN} for request in requests)
    assert all(request["searchString"] == "" for request in requests)
    assert all(request["pageCount"] == 2 for request in requests)
    assert result["record_count"] == 2
    assert str(httpx.URL(NOPRIZ_API_URL)) == NOPRIZ_API_URL


def test_historical_success_false_is_suppressed_as_unknown_projection():
    row = SimpleNamespace(
        inn=EXACT_INN,
        result_status="success",
        is_found=False,
        request_date=date(2026, 9, 29),
        public_records=[{"inn": EXACT_INN, "must_not_project": True}],
        error_code=None,
        error_message=None,
        http_status=200,
    )

    result = nopriz_service._result(row)

    assert result["result"] == "unavailable"
    assert result["checked"] is False
    assert result["reason"] == "nopriz_negative_semantics_unproven"
    assert result["records"] == []
    assert result["record_count"] is None


def test_exact_success_row_is_the_only_service_path_to_found():
    row = SimpleNamespace(
        inn=EXACT_INN,
        result_status="success",
        is_found=True,
        request_date=date(2026, 9, 29),
        public_records=[
            {
                "inn": EXACT_INN,
                "ogrn": "1027700132195",
                "member_status_code": "1",
            }
        ],
        error_code=None,
        error_message=None,
        http_status=200,
    )

    result = nopriz_service._result(row)

    assert result["result"] == "found"
    assert result["record_count"] == 1
    assert result["records"][0]["inn"] == EXACT_INN


def test_current_unknown_row_projects_unavailable_not_not_found():
    row = SimpleNamespace(
        inn=REQUESTED_INN,
        result_status="unknown",
        is_found=None,
        request_date=date(2026, 9, 30),
        public_records=[],
        error_code="exact_identity_not_confirmed",
        error_message=None,
        http_status=200,
    )

    result = nopriz_service._result(row)

    assert result["result"] == "unavailable"
    assert result["checked"] is False
    assert result["reason"] == "exact_identity_not_confirmed"
    assert result["record_count"] is None


def test_worker_persists_unknown_without_false_negative_fields():
    class CaptureSession:
        def execute(self, statement):
            self.params = statement.compile(dialect=postgresql.dialect()).params

    session = CaptureSession()
    published = worker._publish_sro(
        session,
        source_id="nopriz_sro_members_on_demand",
        inn=REQUESTED_INN,
        request_date=date(2026, 9, 30),
        payload={
            "result_status": "unknown",
            "is_found": None,
            "record_count": None,
            "public_records": [],
            "error_code": "exact_identity_not_confirmed",
        },
        now=NOW,
    )

    assert published == 0
    assert session.params["result_status"] == "unknown"
    assert session.params["is_found"] is None
    assert session.params["record_count"] is None
    assert session.params["public_records"] == []
    assert session.params["error_code"] == "exact_identity_not_confirmed"


def test_worker_retains_foreign_raw_before_semantic_decision(monkeypatch, tmp_path):
    foreign = _row(1101, "271304284596", ogrn="301000033330001")
    payload = _payload([foreign])
    page = _candidate(payload)
    observation = {
        "request": page.request,
        "response": page.response_content,
        "headers": {"content-type": "application/json"},
        "status": 200,
        "retrieved_at": NOW.isoformat(),
    }
    monkeypatch.setattr(
        worker,
        "_fetch_nopriz_candidates",
        lambda inn: ((page,), [observation]),
    )
    real_parser = worker.parse_nopriz_candidate_pages
    raw_existed_before_parse = []

    def parse_after_raw(*args, **kwargs):
        raw_paths = list(Path(tmp_path).rglob("exchange.json"))
        raw_existed_before_parse.append(bool(raw_paths))
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(worker, "parse_nopriz_candidate_pages", parse_after_raw)
    reports = []
    context = HandlerContext(
        job_id=uuid4(),
        run_id=uuid4(),
        source_id="nopriz_sro_members_on_demand",
        worker_id="test-worker",
        fencing_token=1,
        deadline_at=NOW + timedelta(minutes=1),
        schedule_metadata={
            "inn": REQUESTED_INN,
            "request_date": "2026-09-30",
            "raw_root": str(tmp_path),
        },
        heartbeat=lambda: None,
        report_counters=reports.append,
        shutdown_requested=lambda: False,
    )

    result = worker.exact_source_handler(context)

    assert raw_existed_before_parse == [True]
    assert result.staging_result.validation.metadata["result"]["result_status"] == "unknown"
    assert result.staging_result.validation.metadata["result"]["public_records"] == []
    raw_path = Path(result.raw_artifacts[0].artifact_reference.removeprefix("file://"))
    exchange = json.loads(raw_path.read_text())
    assert len(exchange["exchanges"]) == 1
    stored = exchange["exchanges"][0]
    assert stored["request"]["filters"] == {"inn": REQUESTED_INN}
    assert stored["http_status"] == 200
    assert stored["response_headers"] == {"content-type": "application/json"}
    assert stored["retrieved_at"] == NOW.isoformat()
    assert len(stored["response_sha256"]) == 64
    assert json.loads(base64.b64decode(stored["response_base64"])) == payload


def test_nostroy_parser_keeps_its_strict_foreign_identity_rejection():
    foreign = _row(1201, "271304284596", ogrn="301000033330001")
    try:
        parse_member_search(_payload([foreign]), REQUESTED_INN)
    except Exception as error:
        assert getattr(error, "kind", None) == "identity_mismatch"
    else:
        raise AssertionError("NOSTROY strict parser must reject a foreign candidate")
