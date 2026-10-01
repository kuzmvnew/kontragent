from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping, Sequence

import httpx


NOPRIZ_API_URL = "https://reestr.nopriz.ru/api/sro/all/member/list"
NOPRIZ_REGISTRY_URL = "https://reestr.nopriz.ru/sro/all/member/list"
NOPRIZ_PAGE_SIZE = 100
NOPRIZ_MAX_PAGES = 20

_INN10 = re.compile(r"\d{10}")
_OGRN13 = re.compile(r"\d{13}")


class NoprizProviderError(Exception):
    def __init__(
        self,
        *,
        kind: str,
        message: str,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class NoprizCandidatePage:
    requested_page: int
    request: dict[str, Any]
    payload: Any
    response_content: bytes
    response_headers: Mapping[str, str]
    http_status: int
    retrieved_at: datetime


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _page_envelope(candidate: NoprizCandidatePage) -> tuple[dict[str, Any] | None, str | None]:
    payload = candidate.payload
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None, "pagination_schema_inconsistent"
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None, "pagination_schema_inconsistent"
    count = _integer(data.get("count"))
    count_pages = _integer(data.get("countPages"))
    page = _integer(data.get("page"))
    if count is None or count < 0 or count_pages is None or count_pages < 0:
        return None, "pagination_schema_inconsistent"
    if page != candidate.requested_page:
        return None, "pagination_page_mismatch"
    return {
        "rows": data["data"],
        "count": count,
        "count_pages": count_pages,
        "page": page,
    }, None


def _unknown(error_code: str) -> dict[str, Any]:
    return {
        "result_status": "unknown",
        "is_found": None,
        "public_records": [],
        "record_count": None,
        "error_code": error_code,
        # Compatibility aliases for callers migrating from the shared SRO
        # parser. None is deliberate: UNKNOWN must never become false.
        "found": None,
        "records": [],
        "total": None,
    }


def _record_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError as error:
        raise NoprizProviderError(
            kind="exact_record_schema_inconsistent",
            message="Некорректная дата в точной записи НОПРИЗ",
        ) from error


def _public_record(row: dict[str, Any], inn: str) -> dict[str, Any]:
    # The source calls this field ``ogrnip`` for both entity types. A company
    # row must nevertheless carry a 13-digit legal-entity OGRN.
    ogrn = row.get("ogrnip")
    if not _OGRN13.fullmatch(str(ogrn or "")):
        raise NoprizProviderError(
            kind="exact_identity_invalid",
            message="Точная запись НОПРИЗ содержит некорректный ОГРН юрлица",
        )
    status = row.get("member_status") or {}
    sro = row.get("sro") or {}
    if not isinstance(status, dict) or not isinstance(sro, dict):
        raise NoprizProviderError(
            kind="exact_record_schema_inconsistent",
            message="Некорректные вложенные поля точной записи НОПРИЗ",
        )
    return {
        "member_id": row.get("id"),
        "inn": inn,
        "ogrn": str(ogrn),
        "registration_number": row.get("registration_number"),
        "inventory_number": row.get("inventory_number"),
        "registry_registration_date": _record_date(row.get("registry_registration_date")),
        "member_status_code": status.get("code"),
        "member_status": status.get("title"),
        "sro_id": sro.get("id"),
        "sro_registration_number": sro.get("registration_number"),
        "sro_name": sro.get("full_description"),
        "last_updated_at": row.get("last_updated_at_date_time_string") or None,
    }


def parse_nopriz_candidate_pages(
    candidates: Sequence[NoprizCandidatePage],
    inn: str,
    *,
    page_size: int = NOPRIZ_PAGE_SIZE,
    max_pages: int = NOPRIZ_MAX_PAGES,
) -> dict[str, Any]:
    """Apply NOPRIZ safe-positive semantics to a bounded candidate sweep.

    The API's INN filter narrows candidates but does not prove equality. Only
    full-string identity matches can enter the public result. Every failure to
    prove a complete traversal therefore remains UNKNOWN.
    """
    if not _INN10.fullmatch(str(inn or "")):
        raise ValueError("Для company-проверки требуется 10-значный ИНН")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise ValueError("page_size должен быть положительным целым числом")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ValueError("max_pages должен быть положительным целым числом")
    if not candidates:
        return _unknown("pagination_incomplete")

    expected_count: int | None = None
    expected_pages: int | None = None
    all_rows: list[dict[str, Any]] = []
    member_ids: set[str] = set()

    for index, candidate in enumerate(candidates, start=1):
        if candidate.requested_page != index:
            return _unknown("pagination_page_mismatch")
        envelope, issue = _page_envelope(candidate)
        if issue:
            return _unknown(issue)
        assert envelope is not None
        count = envelope["count"]
        count_pages = envelope["count_pages"]
        rows = envelope["rows"]

        if expected_count is None:
            expected_count = count
            expected_pages = count_pages
            calculated_pages = math.ceil(count / page_size) if count else 0
            if count_pages != calculated_pages:
                return _unknown("pagination_count_pages_inconsistent")
            if count_pages > max_pages:
                return _unknown("pagination_limit_exceeded")
        elif count != expected_count:
            return _unknown("pagination_count_changed")
        elif count_pages != expected_pages:
            return _unknown("pagination_count_pages_changed")

        if len(rows) > page_size:
            return _unknown("pagination_page_size_invalid")
        if expected_pages:
            expected_size = (
                page_size
                if index < expected_pages
                else expected_count - page_size * (expected_pages - 1)
            )
            if len(rows) != expected_size:
                return _unknown("pagination_page_size_invalid")
        elif rows:
            return _unknown("pagination_page_size_invalid")

        for row in rows:
            if not isinstance(row, dict):
                return _unknown("pagination_schema_inconsistent")
            raw_member_id = row.get("id")
            if raw_member_id is None or not str(raw_member_id).strip():
                return _unknown("pagination_member_id_missing")
            member_id = str(raw_member_id)
            if member_id in member_ids:
                return _unknown("pagination_duplicate_member")
            member_ids.add(member_id)
            all_rows.append(row)

    assert expected_count is not None and expected_pages is not None
    if expected_pages > max_pages:
        return _unknown("pagination_limit_exceeded")
    if len(candidates) != max(1, expected_pages) or len(all_rows) != expected_count:
        return _unknown("pagination_incomplete")

    exact_records: list[dict[str, Any]] = []
    try:
        for row in all_rows:
            # Full-string comparison is intentional. A 12-digit IP INN must
            # never be truncated to a 10-digit company identity.
            if str(row.get("inn") or "") != inn:
                continue
            exact_records.append(_public_record(row, inn))
    except NoprizProviderError as error:
        return _unknown(error.kind)

    if not exact_records:
        return _unknown("exact_identity_not_confirmed")
    return {
        "result_status": "success",
        "is_found": True,
        "public_records": exact_records,
        "record_count": len(exact_records),
        "error_code": None,
        "found": True,
        "records": exact_records,
        "total": len(exact_records),
    }


class NoprizMemberProvider:
    """Bounded candidate transport with exact-identity post-filtering."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        page_size: int = NOPRIZ_PAGE_SIZE,
        max_pages: int = NOPRIZ_MAX_PAGES,
    ):
        self.client = client or httpx.Client(
            timeout=45,
            follow_redirects=True,
            http2=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; bounded candidate lookup)",
                "Referer": NOPRIZ_REGISTRY_URL,
                "Accept": "application/json",
            },
        )
        self.page_size = page_size
        self.max_pages = max_pages

    def fetch_candidate_pages(self, inn: str) -> tuple[NoprizCandidatePage, ...]:
        if not _INN10.fullmatch(str(inn or "")):
            raise ValueError("Для company-проверки требуется 10-значный ИНН")
        pages: list[NoprizCandidatePage] = []
        expected_count: int | None = None
        expected_pages: int | None = None
        for requested_page in range(1, self.max_pages + 1):
            request = {
                "filters": {"inn": inn},
                "searchString": "",
                "page": requested_page,
                "pageCount": self.page_size,
                "sortBy": {},
            }
            try:
                response = self.client.post(NOPRIZ_API_URL, json=request)
            except httpx.TimeoutException as error:
                raise NoprizProviderError(
                    kind="timeout", message="Превышено время ожидания НОПРИЗ"
                ) from error
            except httpx.RequestError as error:
                raise NoprizProviderError(
                    kind="network_error", message="Сетевая ошибка НОПРИЗ"
                ) from error
            if response.status_code in {403, 429}:
                raise NoprizProviderError(
                    kind="source_protection",
                    message=f"Источник вернул HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            if response.status_code != 200:
                raise NoprizProviderError(
                    kind="http_error",
                    message=f"Источник вернул HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            candidate = NoprizCandidatePage(
                requested_page=requested_page,
                request=request,
                payload=payload,
                response_content=response.content,
                response_headers=dict(response.headers),
                http_status=response.status_code,
                retrieved_at=datetime.now(timezone.utc),
            )
            pages.append(candidate)

            envelope, issue = _page_envelope(candidate)
            if issue:
                break
            assert envelope is not None
            if expected_count is None:
                expected_count = envelope["count"]
                expected_pages = envelope["count_pages"]
                if expected_pages > self.max_pages:
                    break
            elif (
                envelope["count"] != expected_count
                or envelope["count_pages"] != expected_pages
            ):
                break
            if expected_pages == 0 or requested_page >= expected_pages:
                break
            if not envelope["rows"]:
                break
        return tuple(pages)

    def check_inn(self, inn: str) -> dict[str, Any]:
        pages = self.fetch_candidate_pages(inn)
        parsed = parse_nopriz_candidate_pages(
            pages, inn, page_size=self.page_size, max_pages=self.max_pages
        )
        return {**parsed, "http_status": 200}
