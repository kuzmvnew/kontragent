from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
import re
from urllib.parse import urljoin

import httpx


BASE_URL = "https://disclosure.1prime.ru"
COMPANY_URL = BASE_URL + "/Portal/Default.aspx?emId={inn}"
_DATE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")


class PrimeDisclosureProviderError(Exception):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


class _PrimeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values: dict[str, list[str]] = {}
        self._capture_id: str | None = None
        self._capture_depth = 0
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self.rows: list[list[dict]] = []
        self.visible: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden += 1
        element_id = attrs.get("id")
        if element_id and element_id.startswith("ctl00_Main_"):
            self._capture_id = element_id
            self._capture_depth = 1
        elif self._capture_id:
            self._capture_depth += 1
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"text": [], "hrefs": []}
        elif tag == "a" and self._cell is not None and attrs.get("href"):
            self._cell["hrefs"].append(attrs["href"])

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._hidden:
            self._hidden -= 1
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None
        if self._capture_id:
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                self._capture_id = None

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if not self._hidden:
            self.visible.append(text)
        if self._capture_id:
            self.values.setdefault(self._capture_id, []).append(text)
        if self._cell is not None:
            self._cell["text"].append(text)


def _first(values: dict[str, list[str]], key: str) -> str | None:
    value = " ".join(values.get(key, [])).strip()
    return value or None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    match = _DATE.search(value)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%d.%m.%Y").date().isoformat()


def parse_prime_disclosure_html(content: str, inn: str) -> dict:
    if not re.fullmatch(r"\d{10}", str(inn or "")):
        raise ValueError("ПРАЙМ disclosure проверяется только по 10-значному ИНН юрлица")
    parser = _PrimeParser()
    parser.feed(content)
    visible = " ".join(parser.visible)
    if "Неверно указан ID эмитента или нет данных" in visible:
        return {"found": False, "profile": None, "documents": [], "document_count": 0}
    returned_inn = _first(parser.values, "ctl00_Main_inn")
    if returned_inn != inn:
        raise PrimeDisclosureProviderError(
            kind="invalid_response",
            message="Страница не подтверждает exact ИНН и не содержит явного empty marker",
        )
    profile = {
        "inn": returned_inn,
        "ogrn": _first(parser.values, "ctl00_Main_ogrn"),
        "full_name": _first(parser.values, "ctl00_Main_fullName"),
        "short_name": _first(parser.values, "ctl00_Main_shortName"),
        "legal_address": _first(parser.values, "ctl00_Main_legalAddress"),
        "postal_address": _first(parser.values, "ctl00_Main_postAddress"),
        "registration_date": _parse_date(_first(parser.values, "ctl00_Main_regDate")),
        "registration_authority": _first(parser.values, "ctl00_Main_regAgency"),
        "fsfr_code": _first(parser.values, "ctl00_Main_fsfr"),
        "okpo": _first(parser.values, "ctl00_Main_okpo"),
    }
    documents = []
    seen = set()
    for row in parser.rows:
        href = next(
            (
                href
                for cell in row
                for href in cell["hrefs"]
                if "/Portal/GetDocument.aspx" in href and f"emId={inn}" in href
            ),
            None,
        )
        if not href:
            continue
        absolute_url = urljoin(BASE_URL, href)
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        texts = [cell["text"] for cell in row if cell["text"]]
        title = next(
            (
                text for text in texts
                if not text.isdigit()
                and not _DATE.fullmatch(text)
                and not text.lower().startswith("загрузить")
            ),
            "Раскрытый документ",
        )
        dates = [match.group(1) for text in texts for match in _DATE.finditer(text)]
        documents.append(
            {
                "title": title,
                "publication_date": _parse_date(dates[-1]) if dates else None,
                "source_url": absolute_url,
            }
        )
    return {
        "found": True,
        "profile": profile,
        "documents": documents[:100],
        "document_count": len(documents),
    }


class PrimeDisclosureProvider:
    def __init__(self, client=None):
        self.client = client or httpx.Client(
            timeout=30,
            follow_redirects=True,
            http2=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; low-load exact-INN disclosure lookup)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    def check_inn(self, inn: str) -> dict:
        if not re.fullmatch(r"\d{10}", str(inn or "")):
            raise ValueError("ПРАЙМ disclosure проверяется только по 10-значному ИНН юрлица")
        url = COMPANY_URL.format(inn=inn)
        try:
            response = self.client.get(url)
        except httpx.TimeoutException as error:
            raise PrimeDisclosureProviderError(kind="timeout", message="Превышено время ожидания ПРАЙМ") from error
        except httpx.RequestError as error:
            raise PrimeDisclosureProviderError(kind="network_error", message="Сетевая ошибка ПРАЙМ") from error
        if response.status_code in {403, 429}:
            raise PrimeDisclosureProviderError(kind="source_protection", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code != 200:
            raise PrimeDisclosureProviderError(kind="http_error", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        try:
            content = response.content.decode("windows-1251")
        except UnicodeDecodeError as error:
            raise PrimeDisclosureProviderError(kind="invalid_encoding", message="Ответ ПРАЙМ не декодируется как Windows-1251", http_status=200) from error
        return {**parse_prime_disclosure_html(content, inn), "http_status": 200, "source_url": url}
