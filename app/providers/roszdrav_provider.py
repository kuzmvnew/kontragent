from __future__ import annotations

from html import unescape
from pathlib import Path
import re
import ssl

import httpx


LICENSE_SEARCH_URL = "https://roszdravnadzor.gov.ru/ajax/services/licenses"
MEDICAL_DEVICE_SEARCH_URL = (
    "https://elk.roszdravnadzor.gov.ru/public-gateway/"
    "registered-med-product/api/v1/med-product/filter-public"
)
REQUEST_TIMEOUT_SECONDS = 60.0
_TAG_RE = re.compile(r"<[^>]+>")
RUSSIAN_TRUSTED_ROOT_CA = (
    Path(__file__).resolve().parents[2] / "certs" / "russian_trusted_root_ca.pem"
)


class RoszdravProviderError(Exception):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


def normalize_inn(value) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[0-9]{10}|[0-9]{12}", text) else ""


def _text(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("label")
    if value is None:
        return None
    value = unescape(_TAG_RE.sub("", str(value))).strip()
    return value or None


def parse_unified_license_payload(payload: dict, requested_inn: str) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RoszdravProviderError(
            kind="invalid_response",
            message="Росздравнадзор вернул некорректный JSON поиска лицензий",
        )
    records = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise RoszdravProviderError(
                kind="invalid_response", message="Некорректная строка реестра лицензий"
            )
        inn = normalize_inn(_text(row.get("col7")))
        if inn != requested_inn:
            raise RoszdravProviderError(
                kind="identity_mismatch",
                message="Росздравнадзор вернул лицензию для другого ИНН",
            )
        records.append({
            "record_id": row.get("DT_RowId"),
            "registration_number": _text(row.get("col1")),
            "registration_date": _text(row.get("col2")),
            "licensee_name": _text(row.get("col3")),
            "authority_name": _text(row.get("col4")),
            "address": _text(row.get("col5")),
            "ogrn": _text(row.get("col6")),
            "inn": inn,
            "okpo": _text(row.get("col8")),
            "license_number": _text(row.get("col9")),
            "decision_info": _text(row.get("col10")),
            "start_date": _text(row.get("col11")),
            "end_date": _text(row.get("col12")),
            "change_info": _text(row.get("col13")),
            "suspension_info": _text(row.get("col14")),
            "resumption_info": _text(row.get("col15")),
            "cancellation_info": _text(row.get("col16")),
            "termination_info": _text(row.get("col17")),
            "document_info": _text(row.get("col18")),
            "work_places": row.get("objects") or [],
        })
    total = payload.get("recordsFiltered", payload.get("recordsTotal", len(records)))
    if not isinstance(total, int) or total < len(records):
        raise RoszdravProviderError(
            kind="invalid_response", message="Некорректное количество строк реестра лицензий"
        )
    return {"found": bool(records), "records": records, "total": total}


class _RoszdravHttpProvider:
    def __init__(self, client=None, *, verify: bool = True):
        self._external_client = client
        self.verify = verify

    def _client(self):
        if self._external_client is not None:
            return self._external_client, False
        return httpx.Client(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            follow_redirects=True,
            verify=self.verify,
            headers={"User-Agent": "Kontragent/1.0 official-source-check"},
        ), True

    @staticmethod
    def _request_error(error):
        if isinstance(error, httpx.TimeoutException):
            return RoszdravProviderError(kind="timeout", message="Превышено время ожидания Росздравнадзора")
        return RoszdravProviderError(kind="network_error", message="Сетевая ошибка запроса к Росздравнадзору")


class RoszdravUnifiedLicenseProvider(_RoszdravHttpProvider):
    def check_inn(self, inn: str) -> dict:
        inn = normalize_inn(inn)
        if not inn:
            raise ValueError("Для проверки нужен корректный ИНН ЮЛ или ИП")
        client, close = self._client()
        try:
            try:
                response = client.post(LICENSE_SEARCH_URL, data={
                    "draw": "1", "start": "0", "length": "100",
                    "prev_total": "0", "q_no": "", "q_activity": "",
                    "q_registry": "0", "i_am_human": "1", "q_type": "",
                    "dt_from": "", "dt_to": "", "q_org_ogrn": "",
                    "q_org_label": "", "q_region": "", "q_active": "1",
                    "q_org_inn": inn,
                })
            except httpx.RequestError as error:
                raise self._request_error(error) from error
            if response.status_code != 200:
                raise RoszdravProviderError(
                    kind="service_unavailable" if response.status_code >= 500 else "http_error",
                    message=f"Росздравнадзор вернул HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise RoszdravProviderError(kind="invalid_response", message="Росздравнадзор вернул не JSON", http_status=200) from error
            if payload.get("message") == "Документов не найдено.":
                payload = {
                    **payload,
                    "data": [],
                    "recordsFiltered": 0,
                    "recordsTotal": 0,
                }
            elif payload.get("message"):
                raise RoszdravProviderError(kind="source_protection", message=str(payload["message"]), http_status=200)
            parsed = parse_unified_license_payload(payload, inn)
            if parsed["total"] > len(parsed["records"]):
                raise RoszdravProviderError(kind="response_truncated", message="Ответ поиска лицензий не поместился в один low-load запрос", http_status=200)
            return {**parsed, "raw_payload": payload, "http_status": 200}
        finally:
            if close:
                client.close()


def parse_medical_device_payload(payload: dict, registration_number: str) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        raise RoszdravProviderError(kind="invalid_response", message="Некорректный JSON реестра медизделий")
    records = payload["content"]
    matching = [row for row in records if str(row.get("noRu") or "").strip().casefold() == registration_number.casefold()]
    total = payload.get("totalElements")
    if not isinstance(total, int):
        raise RoszdravProviderError(kind="invalid_response", message="В реестре медизделий нет totalElements")
    if total and len(matching) != total:
        raise RoszdravProviderError(kind="identity_mismatch", message="Поиск медизделия вернул неточное совпадение")
    return {"found": bool(matching), "records": matching, "total": total}


class RoszdravMedicalDeviceProvider(_RoszdravHttpProvider):
    def __init__(self, client=None, *, verify: bool = True):
        if client is None and verify is True:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=str(RUSSIAN_TRUSTED_ROOT_CA))
            verify = context
        super().__init__(client=client, verify=verify)

    def check_registration_number(self, registration_number: str) -> dict:
        registration_number = str(registration_number or "").strip()
        if not registration_number:
            raise ValueError("Номер регистрационного удостоверения обязателен")
        client, close = self._client()
        try:
            try:
                response = client.post(
                    MEDICAL_DEVICE_SEARCH_URL,
                    params={"page": 0, "size": 100},
                    json={"noRu": registration_number},
                )
            except httpx.RequestError as error:
                raise self._request_error(error) from error
            if response.status_code != 200:
                raise RoszdravProviderError(
                    kind="service_unavailable" if response.status_code >= 500 else "http_error",
                    message=f"Реестр медизделий вернул HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise RoszdravProviderError(kind="invalid_response", message="Реестр медизделий вернул не JSON", http_status=200) from error
            parsed = parse_medical_device_payload(payload, registration_number)
            return {**parsed, "raw_payload": payload, "http_status": 200}
        finally:
            if close:
                client.close()
