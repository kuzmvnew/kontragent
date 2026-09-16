from __future__ import annotations

from html.parser import HTMLParser
import re

import httpx

PD_OPERATOR_URL = "https://pd.rkn.gov.ru/operators-registry/operators-list/"
_INN10 = re.compile(r"(?<!\d)\d{10}(?!\d)")


class RoskomnadzorProviderError(Exception):
    def __init__(self, *, kind, message, http_status=None):
        super().__init__(message)
        self.kind, self.message, self.http_status = kind, message, http_status


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))


def parse_pd_operator_html(content: str, inn: str) -> dict:
    if not re.fullmatch(r"\d{10}", inn):
        raise ValueError("Для company-проверки требуется 10-значный ИНН")
    parser = _VisibleText()
    parser.feed(content)
    text = " ".join(parser.parts)
    lowered = text.lower()
    if any(marker in lowered for marker in ("captcha", "капча", "доступ ограничен", "слишком много запросов")):
        raise RoskomnadzorProviderError(kind="source_protection", message="Источник включил защиту запросов")
    empty_markers = ("записей не найдено", "ничего не найдено", "по вашему запросу ничего не найдено")
    found_inns = set(_INN10.findall(text))
    if inn not in found_inns:
        if any(marker in lowered for marker in empty_markers):
            return {"found": False, "records": [], "total": 0}
        raise RoskomnadzorProviderError(kind="invalid_response", message="Ответ не подтверждает exact ИНН и не содержит признак пустого результата")
    # Only non-personal evidence crosses the provider boundary. The registry
    # number is deliberately optional because official markup has changed.
    reg_numbers = sorted(set(re.findall(r"\b\d{2}-\d{2}-\d{4,8}\b", text)))
    return {"found": True, "records": [{"inn": inn, "registry_number": number} for number in reg_numbers] or [{"inn": inn, "registry_number": None}], "total": max(1, len(reg_numbers))}


class RoskomnadzorPdOperatorProvider:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=30, follow_redirects=True, http2=False, headers={"User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; low-load exact-INN lookup)", "Referer": PD_OPERATOR_URL, "Accept": "text/html,application/xhtml+xml"})

    def check_inn(self, inn: str):
        if not re.fullmatch(r"\d{10}", str(inn or "")):
            raise ValueError("Для company-проверки требуется 10-значный ИНН")
        try:
            response = self.client.get(PD_OPERATOR_URL, params={"act": "search", "name_full": "", "inn": inn, "regn": ""})
        except httpx.TimeoutException as error:
            raise RoskomnadzorProviderError(kind="timeout", message="Превышено время ожидания Роскомнадзора") from error
        except httpx.RequestError as error:
            raise RoskomnadzorProviderError(kind="network_error", message="Сетевая ошибка Роскомнадзора") from error
        if response.status_code in {403, 429}:
            raise RoskomnadzorProviderError(kind="source_protection", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code != 200:
            raise RoskomnadzorProviderError(kind="http_error", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        parsed = parse_pd_operator_html(response.text, inn)
        return {**parsed, "http_status": 200}
