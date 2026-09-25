"""Pure parser for authorized public Firmoteka company pages.

The parser deliberately keeps the bridge provenance.  Values asserted by a
Firmoteka page are useful enrichment, but they are not silently promoted to an
official-registry assertion.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import html as html_lib
from html.parser import HTMLParser
import json
import re
from typing import Any


PARSER_VERSION = "firmoteka-public-page-v1"


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(html_lib.unescape(str(value)).replace("\xa0", " ").split()) or None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.json_scripts: list[str] = []
        self._capture_json = False
        self._json_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._capture_json = True
            self._json_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capture_json:
            self.json_scripts.append("".join(self._json_buffer))
            self._capture_json = False

    def handle_data(self, data: str) -> None:
        if self._capture_json:
            self._json_buffer.append(data)
        elif data.strip():
            self.text.append(data)


def _decode_nuxt_company(decoded_html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json"[^>]*id="__NUXT_DATA__">(.*?)</script>',
        decoded_html,
        re.I | re.S,
    )
    if not match:
        return {}
    try:
        values = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(values, list):
        return {}
    memo: dict[int, Any] = {}
    wrappers = {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}

    def dereference(value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(values):
            return decode_index(value)
        return value

    def decode_index(index: int) -> Any:
        if index in memo:
            return memo[index]
        value = values[index]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in wrappers:
                return dereference(value[1])
            if value and value[0] == "Set":
                return [dereference(item) for item in value[1:]]
            result: list[Any] = []
            memo[index] = result
            result.extend(dereference(item) for item in value)
            return result
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            memo[index] = result
            result.update({key: dereference(item) for key, item in value.items()})
            return result
        return value

    try:
        root = decode_index(0)
        state = root.get("state", {})
        query_state = next(
            (value for key, value in state.items() if key.endswith("vue-query")),
            {},
        )
        for query in query_state.get("queries", []):
            query_key = query.get("queryKey") or []
            if query_key and query_key[0] == "company-tags":
                company = query.get("state", {}).get("data")
                return company if isinstance(company, dict) else {}
    except (AttributeError, IndexError, KeyError, TypeError):
        return {}
    return {}


def _data_value(company: dict[str, Any], name: str) -> Any:
    for section in company.get("data") or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("values") or []:
            if isinstance(item, dict) and item.get("name") == name:
                return item.get("value")
    return None


def _date(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_firmoteka_page(
    raw: bytes, *, requested_inn: str, url: str, fetched_at: datetime
) -> dict[str, Any]:
    decoded = raw.decode("utf-8", errors="replace")
    company_payload = _decode_nuxt_company(decoded)
    parser = _PageParser()
    parser.feed(decoded)
    page_text = clean_text(" ".join(parser.text)) or ""
    objects: list[dict[str, Any]] = []
    for payload in parser.json_scripts:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    org = next(
        (item for item in objects if item.get("@type") in {"Organization", "Person"}),
        {},
    )
    rendered_inn = clean_text(company_payload.get("tin") or org.get("taxID"))
    if not rendered_inn:
        match = re.search(r"(?:ИНН\s*</?[^>]*>\s*|ИНН\s+)(\d{10}|\d{12})", decoded, re.I)
        rendered_inn = match.group(1) if match else None
    payload_status = company_payload.get("status")
    payload_status = payload_status if isinstance(payload_status, dict) else {}
    status_text = clean_text(payload_status.get("name"))
    if not status_text:
        match = re.search(
            r"(Действующая организация|Действующий индивидуальный предприниматель|"
            r"Ликвидирована|Исключена из ЕГРЮЛ|В процессе ликвидации|"
            r"Реорганизация|Прекратил деятельность)",
            page_text,
            re.I,
        )
        status_text = clean_text(match.group(1)) if match else None
    status_key = (status_text or "").casefold()
    status = next(
        (
            normalized
            for marker, normalized in (
                ("действующ", "ACTIVE"),
                ("ликвидир", "LIQUIDATING"),
                ("ликвидирован", "LIQUIDATED"),
                ("исключен", "EXCLUDED"),
                ("реорган", "REORGANIZATION"),
                ("прекратил", "TERMINATED"),
            )
            if marker in status_key
        ),
        clean_text(payload_status.get("code")),
    )
    address = org.get("address") if isinstance(org.get("address"), dict) else {}
    okved_raw = clean_text(company_payload.get("okved"))
    okved_match = re.match(r"([0-9.]+)\s*(.*)", okved_raw or "")
    managers = _data_value(company_payload, "Руководство")
    manager_items = managers.get("items", []) if isinstance(managers, dict) else []
    manager = manager_items[0] if manager_items and isinstance(manager_items[0], dict) else {}
    enforcements = company_payload.get("enforcements")
    enforcements = enforcements if isinstance(enforcements, dict) else {}
    result: dict[str, Any] = {
        "requested_inn": requested_inn,
        "rendered_inn": rendered_inn,
        "identity_match": rendered_inn == requested_inn,
        "url": url,
        "fetched_at": fetched_at.isoformat(),
        "page_sha256": hashlib.sha256(raw).hexdigest(),
        "name": clean_text(company_payload.get("short_name") or org.get("name") or org.get("legalName")),
        "full_name": clean_text(company_payload.get("full_name") or org.get("legalName")),
        "ogrn": clean_text(company_payload.get("psrn") or ((org.get("identifier") or {}).get("value") if isinstance(org.get("identifier"), dict) else None)),
        "kpp": clean_text(_data_value(company_payload, "КПП")),
        "entity_type": "individual_entrepreneur" if len(requested_inn) == 12 else "legal",
        "status_text": status_text,
        "status_normalized": status,
        "legal_form": clean_text(company_payload.get("legal_form")),
        "registration_date": _date(company_payload.get("registration_date") or org.get("foundingDate")),
        "termination_date": _date(company_payload.get("termination_date") or org.get("dissolutionDate")),
        "address": clean_text(company_payload.get("legal_address") or address.get("streetAddress")),
        "region": clean_text(company_payload.get("town") or address.get("addressRegion") or address.get("addressLocality")),
        "okved": okved_match.group(1) if okved_match else None,
        "okved_name": clean_text(okved_match.group(2)) if okved_match else None,
        "manager": clean_text(manager.get("name")),
        "manager_position": clean_text(manager.get("position")),
        "founders": _data_value(company_payload, "Учредители"),
        "authorized_capital": _data_value(company_payload, "Уставный капитал"),
        "financials": company_payload.get("revenues") or {},
        "tax_debts": company_payload.get("tax_debts") or [],
        "taxes_paid": company_payload.get("taxes_paid") or [],
        "licenses": company_payload.get("licenses") or [],
        "divisions": company_payload.get("divisions") or [],
        "events": company_payload.get("events") or [],
        "employee_counts": company_payload.get("employee_counts") or company_payload.get("msp_employee_counts") or {},
        "contacts": company_payload.get("contacts") or {},
        "enforcements": enforcements,
        "fssp_count": enforcements.get("count"),
        "fssp_remaining_amount": enforcements.get("total_rest"),
        "source": "firmoteka_authorized_bridge",
        "provenance": "AUTHORIZED_BRIDGE",
    }
    fact_fields = (
        "name", "full_name", "ogrn", "kpp", "entity_type", "status_normalized",
        "registration_date", "termination_date", "address", "region", "okved",
        "manager", "manager_position", "founders", "authorized_capital",
        "financials", "tax_debts", "taxes_paid", "licenses", "divisions",
        "events", "employee_counts", "contacts", "enforcements",
    )
    result["facts"] = [
        {
            "field_name": field,
            "value": result[field],
            "source": "firmoteka_authorized_bridge",
            "source_url": url,
            "fetched_at": fetched_at.isoformat(),
            "confidence": "MEDIUM",
            "page_sha256": result["page_sha256"],
        }
        for field in fact_fields
        if result.get(field) not in (None, "", [], {})
    ]
    return result


def source_as_of(projection: dict[str, Any]) -> date | None:
    values: list[date] = []
    for key in ("registration_date", "termination_date"):
        value = projection.get(key)
        if value:
            try:
                values.append(date.fromisoformat(str(value)))
            except ValueError:
                pass
    return max(values) if values else None
