from __future__ import annotations

from html.parser import HTMLParser
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urljoin

import httpx


MOSCOW_BASE_URL = "https://mos-gorsud.ru"
MOSCOW_SEARCH_URL = MOSCOW_BASE_URL + "/search"


class GeneralCourtProvider(Protocol):
    """Vendor-neutral contract for targeted general-jurisdiction court checks."""

    code: str

    def search_company(self, *, inn: str, ogrn: str | None, full_name: str) -> dict: ...


class GeneralCourtProviderError(Exception):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


class _MoscowRowsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._cell: list[str] | None = None
        self.visible: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden += 1
        if tag == "tr" and attrs.get("data-href"):
            self._row = {"href": attrs["data-href"], "cells": []}
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._hidden:
            self._hidden -= 1
        if tag == "td" and self._cell is not None and self._row is not None:
            self._row["cells"].append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if not self._hidden:
            self.visible.append(text)
        if self._cell is not None:
            self._cell.append(text)


def _normalized_name(value: str) -> str:
    value = value.upper().replace("Ё", "Е")
    value = value.replace("«", '"').replace("»", '"')
    return re.sub(r"[^А-ЯA-Z0-9]+", " ", value).strip()


def _case_number(value: str) -> str | None:
    value = value.split("Скопировать", 1)[0].strip()
    return value or None


def _first_date(value: str) -> str | None:
    match = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", value or "")
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else None


def _role_for_name(parties: str, full_name: str) -> str | None:
    target = _normalized_name(full_name)
    segments = re.split(r"\b(Истец|Ответчик|Третье лицо|Заинтересованное лицо)\b", parties)
    for index in range(1, len(segments) - 1, 2):
        if target and target in _normalized_name(segments[index + 1]):
            return {
                "Истец": "claimant",
                "Ответчик": "defendant",
                "Третье лицо": "third_party",
                "Заинтересованное лицо": "interested_party",
            }.get(segments[index])
    return None


def parse_moscow_court_search_html(content: str, *, full_name: str) -> dict:
    parser = _MoscowRowsParser()
    parser.feed(content)
    visible = " ".join(parser.visible)
    if "Подтвердите, что вы не робот" in visible or "SmartCaptcha" in visible:
        raise GeneralCourtProviderError(kind="challenge_required", message="Официальный портал запросил human challenge")

    cases = []
    for row in parser.rows:
        cells = row["cells"]
        if len(cells) < 6:
            continue
        parties = cells[1]
        normalized_target = _normalized_name(full_name)
        normalized_parties = _normalized_name(parties)
        exact_name = bool(normalized_target and normalized_target in normalized_parties)
        if not exact_name:
            continue
        role = _role_for_name(parties, full_name)
        cases.append(
            {
                "case_number": _case_number(cells[0]),
                "court": cells[5] or None,
                "category": cells[3] or None,
                "role": role,
                "parties": parties,
                "stage": cells[2] or None,
                "event_date": _first_date(cells[2]),
                "result": cells[6] if len(cells) > 6 and cells[6] else None,
                "result_date": _first_date(cells[6]) if len(cells) > 6 else None,
                "source_url": urljoin(MOSCOW_BASE_URL, row["href"]),
                "matching_method": "exact_full_legal_name",
                "confidence": "medium",
                "evidence": {
                    "official_portal": True,
                    "matched_name": full_name,
                    "identifier_in_result": False,
                },
            }
        )
    return {
        "cases": cases,
        "result_count": len(cases),
        "coverage": {
            "coverage_source": "moscow_courts_official",
            "regions_checked": ["Москва"],
            "portals_checked": [MOSCOW_SEARCH_URL],
            "matching_confidence": "medium" if cases else "not_confirmed",
            "coverage_label": "MOSCOW TARGETED QUERY (FIRST 100) / OTHER REGIONS NOT CHECKED",
        },
    }


class MoscowCourtProvider:
    code = "moscow_courts_official"

    def __init__(self, client=None):
        self.client = client or httpx.Client(
            timeout=45,
            follow_redirects=True,
            http2=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; targeted company lookup)"},
        )

    def search_company(self, *, inn: str, ogrn: str | None, full_name: str) -> dict:
        if not full_name.strip():
            raise ValueError("Для официального поиска Москвы требуется полное наименование")
        params = {"participant": full_name.strip(), "limit": 100, "page": 1}
        url = MOSCOW_SEARCH_URL + "?" + urlencode(params)
        try:
            response = self.client.get(MOSCOW_SEARCH_URL, params=params)
        except httpx.TimeoutException as error:
            raise GeneralCourtProviderError(kind="timeout", message="Превышено время ожидания портала судов Москвы") from error
        except httpx.RequestError as error:
            raise GeneralCourtProviderError(kind="network_error", message="Сетевая ошибка портала судов Москвы") from error
        if response.status_code in {403, 429}:
            raise GeneralCourtProviderError(kind="source_protection", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code != 200:
            raise GeneralCourtProviderError(kind="http_error", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        return {**parse_moscow_court_search_html(response.text, full_name=full_name), "source_url": url, "http_status": 200}


class RegionalSudrfProvider:
    """Shared official sudrf form contract; execution is deliberately one configured court at a time."""

    code = "regional_sudrf_official"
    exact_identifier_fields = {"inn": "G2_PARTS__INN_STRSS", "ogrn": "G2_PARTS__OGRN_STRSS"}

    def __init__(self, *, base_url: str | None = None, delo_id: str = "1540005"):
        self.base_url = base_url
        self.delo_id = delo_id

    @classmethod
    def build_exact_identifier_params(cls, *, inn: str, ogrn: str | None = None, delo_id: str) -> dict:
        params = {"name": "sud_delo", "name_op": "sf", "new": "5", "delo_id": delo_id}
        if inn:
            params[cls.exact_identifier_fields["inn"]] = inn
        if ogrn:
            params[cls.exact_identifier_fields["ogrn"]] = ogrn
        return params

    def build_search_url(self, *, inn: str, ogrn: str | None = None) -> str:
        if not self.base_url:
            raise ValueError("Regional sudrf target не настроен")
        params = self.build_exact_identifier_params(inn=inn, ogrn=ogrn, delo_id=self.delo_id)
        return self.base_url + "?" + urlencode(params)


class GasPravosudieProvider:
    code = "gas_pravosudie_official"
    access_status = "central_search_technical_access_unconfirmed"


class CentralGasProvider(GasPravosudieProvider):
    pass


class BSRProvider:
    code = "bsr_sudrf_official"
    source_url = "https://bsr.sudrf.ru/bigs/portal.html"
    access_status = "timeout_observed"


class SudactDiscoveryProvider:
    code = "sudact_public_discovery"
    source_of_truth = False


@dataclass(frozen=True)
class GeneralCourtRoute:
    region_code: str | None
    region_name: str
    provider_code: str
    portal_url: str
    coverage_status: str


REGIONAL_TARGETS = {
    "78": GeneralCourtRoute("78", "Санкт-Петербург", "regional_sudrf_official", "https://sankt-peterburgsky--spb.sudrf.ru/modules.php", "targeted_regional_portal"),
    "66": GeneralCourtRoute("66", "Свердловская область", "regional_sudrf_official", "https://oblsud--svd.sudrf.ru/modules.php", "targeted_regional_portal"),
    "16": GeneralCourtRoute("16", "Республика Татарстан", "regional_sudrf_official", "https://vs--tat.sudrf.ru/modules.php", "targeted_regional_portal"),
    "54": GeneralCourtRoute("54", "Новосибирская область", "regional_sudrf_official", "https://oblsud--nsk.sudrf.ru/modules.php", "targeted_regional_portal"),
}


class GeneralCourtRouter:
    """Selects one low-load official target from company region; never fans out nationally."""

    def route(self, region_code: str | None) -> tuple[GeneralCourtRoute, object]:
        code = str(region_code or "").zfill(2)
        if code == "77":
            route = GeneralCourtRoute("77", "Москва", "moscow_courts_official", MOSCOW_SEARCH_URL, "targeted_city_portal")
            return route, MoscowCourtProvider()
        route = REGIONAL_TARGETS.get(code)
        if route:
            return route, RegionalSudrfProvider(base_url=route.portal_url)
        fallback = GeneralCourtRoute(code or None, "Регион не настроен", "gas_pravosudie_official", BSRProvider.source_url, "central_access_unconfirmed")
        return fallback, CentralGasProvider()
