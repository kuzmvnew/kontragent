from __future__ import annotations

from datetime import datetime
import xml.etree.ElementTree as ET

import httpx


SERVICE_URL = "https://www.cbr.ru/FO_ZoomWS/FinOrg.asmx"
SOAP_ACTION_BASE = "http://web.cbr.ru/"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WEB_NS = "http://web.cbr.ru/"
REQUEST_TIMEOUT_SECONDS = 45.0


class CbrFinorgProviderError(Exception):
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


def normalize_inn(value) -> str:
    return "".join(
        symbol
        for symbol in str(value or "")
        if symbol.isdigit()
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_first(root, name):
    for element in root.iter():
        if _local_name(element.tag) == name:
            return element
    return None


def _child_text(element, name):
    if element is None:
        return None

    for child in element:
        if _local_name(child.tag) == name:
            value = child.text
            if value is None:
                return None
            value = value.strip()
            return value or None

    return None


def _descendants(element, name):
    if element is None:
        return []

    return [
        item
        for item in element.iter()
        if item is not element
        and _local_name(item.tag) == name
    ]


def _parse_bool(value):
    if value is None:
        return None

    text = str(value).strip().lower()

    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False

    return None


def _parse_int(value):
    if value is None:
        return None

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_date(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        try:
            parsed = datetime.strptime(
                text[:10],
                "%d.%m.%Y",
            )
        except ValueError:
            return None

    if parsed.year <= 1:
        return None

    return parsed.date().isoformat()


def _parse_xml(content: bytes):
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise CbrFinorgProviderError(
            kind="invalid_response",
            message=(
                "Банк России вернул некорректный SOAP XML"
            ),
        ) from error

    fault = _find_first(root, "Fault")

    if fault is not None:
        message = (
            _child_text(fault, "faultstring")
            or "SOAP Fault"
        )

        raise CbrFinorgProviderError(
            kind="source_error",
            message=f"Банк России: {message}",
        )

    return root


def parse_search_by_inns_response(
    content: bytes,
    *,
    requested_inn: str,
) -> dict:
    root = _parse_xml(content)
    result = _find_first(
        root,
        "SearchByINNsResult",
    )

    if result is None:
        raise CbrFinorgProviderError(
            kind="invalid_response",
            message=(
                "В ответе Банка России отсутствует "
                "SearchByINNsResult"
            ),
        )

    success = _parse_bool(
        _child_text(result, "IsSucess")
    )
    error_text = _child_text(result, "Error")

    if success is False:
        raise CbrFinorgProviderError(
            kind="source_error",
            message=(
                error_text
                or "Банк России не выполнил поиск по ИНН"
            ),
        )

    records = []

    for item in _descendants(result, "Record"):
        record = {
            "cbr_id": _parse_int(
                _child_text(item, "Id")
            ),
            "ogrn": _child_text(item, "OGRN"),
            "inn": normalize_inn(
                _child_text(item, "INN")
            ),
            "name": _child_text(item, "Name"),
            "status": _child_text(item, "Status"),
            "error_text": _child_text(
                item,
                "ErrorText",
            ),
        }
        records.append(record)

    matching = next(
        (
            record
            for record in records
            if record["inn"] == requested_inn
            and (
                (record["cbr_id"] or 0) > 0
                or bool(record["ogrn"])
                or bool(record["name"])
            )
        ),
        None,
    )

    return {
        "found": matching is not None,
        "record": matching,
        "records_count": len(records),
        "source_error": error_text,
    }


def parse_full_info_response(
    content: bytes,
    *,
    requested_inn: str,
) -> dict:
    root = _parse_xml(content)
    result = _find_first(
        root,
        "GetFullInfoByINNResult",
    )

    if result is None:
        raise CbrFinorgProviderError(
            kind="invalid_response",
            message=(
                "В ответе Банка России отсутствует "
                "GetFullInfoByINNResult"
            ),
        )

    response_inn = normalize_inn(
        _child_text(result, "INN")
    )

    if response_inn and response_inn != requested_inn:
        raise CbrFinorgProviderError(
            kind="identity_mismatch",
            message=(
                "Банк России вернул сведения для другого ИНН"
            ),
        )

    fo_types_container = _find_first(
        result,
        "FOTypes",
    )
    fo_types = []

    if fo_types_container is not None:
        for item in fo_types_container:
            if _local_name(item.tag) == "string":
                value = (item.text or "").strip()
                if value and value not in fo_types:
                    fo_types.append(value)

    licenses = []
    license_container = _find_first(
        result,
        "LicList",
    )

    if license_container is not None:
        for item in _descendants(
            license_container,
            "LicInfo",
        ):
            licenses.append(
                {
                    "activity_id": _parse_int(
                        _child_text(item, "VidID")
                    ),
                    "activity": _child_text(item, "VidD"),
                    "number": _child_text(
                        item,
                        "LIC_Number",
                    ),
                    "name": _child_text(
                        item,
                        "LIC_Name",
                    ),
                    "start_date": _normalize_date(
                        _child_text(
                            item,
                            "LIC_DTStart",
                        )
                    ),
                    "end_date": _normalize_date(
                        _child_text(
                            item,
                            "LIC_DTEnd",
                        )
                    ),
                }
            )

    payment_systems = []
    payment_container = _find_first(
        result,
        "PaymentSystems",
    )

    if payment_container is not None:
        for item in _descendants(
            payment_container,
            "PaymentSystem",
        ):
            payment_systems.append(
                {
                    "name": _child_text(item, "Name"),
                    "start_date": _normalize_date(
                        _child_text(item, "DateStart")
                    ),
                    "end_date": _normalize_date(
                        _child_text(item, "DateFinish")
                    ),
                }
            )

    websites = []
    website_container = _find_first(
        result,
        "WebSites",
    )

    if website_container is not None:
        for item in website_container:
            if _local_name(item.tag) == "string":
                value = (item.text or "").strip()
                if value and value not in websites:
                    websites.append(value)

    mfo_history = []
    mfo_container = _find_first(
        result,
        "MFOList",
    )

    if mfo_container is not None:
        for item in _descendants(
            mfo_container,
            "MFO",
        ):
            mfo_history.append(
                {
                    "kind": _child_text(item, "MFOVid"),
                    "start_date": _normalize_date(
                        _child_text(item, "DateBegin")
                    ),
                    "end_date": _normalize_date(
                        _child_text(item, "DateEnd")
                    ),
                }
            )

    participant = {
        "cbr_id": _parse_int(
            _child_text(result, "ID")
        ),
        "ogrn": _child_text(result, "OGRN"),
        "inn": response_inn or requested_inn,
        "short_name": _child_text(
            result,
            "ShortName",
        ),
        "name": _child_text(result, "Name"),
        "english_name": _child_text(
            result,
            "EngName",
        ),
        "address": _child_text(result, "Address"),
        "phones": _child_text(result, "Phones"),
        "email": _child_text(result, "Email"),
        "okato": _parse_int(
            _child_text(result, "OKATO")
        ),
        "region": _child_text(result, "Reg"),
        "fo_types": fo_types,
        "status": _child_text(result, "Status"),
        "is_sro_member": _parse_bool(
            _child_text(result, "IsSroMember")
        ),
        "is_rss": _parse_bool(
            _child_text(result, "IsRss")
        ),
        "npo_flag": _parse_bool(
            _child_text(result, "NPO_FLG")
        ),
        "asv_flag": _parse_bool(
            _child_text(result, "ASV_FLG")
        ),
        "pay_system": _child_text(
            result,
            "PaySystem",
        ),
        "regnum": _child_text(result, "REGNUM"),
        "bic": _child_text(result, "BIC"),
        "licenses": licenses,
        "payment_systems": payment_systems,
        "websites": websites,
        "mfo_history": mfo_history,
        "has_branches": _parse_bool(
            _child_text(result, "HasBranches")
        ),
        "source_error": _child_text(result, "Error"),
        "bank_status": _child_text(
            result,
            "BnkStatus",
        ),
        "registration_date": _normalize_date(
            _child_text(result, "RegistrationDate")
        ),
    }

    if not (
        participant["cbr_id"]
        or participant["name"]
        or participant["short_name"]
        or participant["fo_types"]
        or participant["licenses"]
    ):
        raise CbrFinorgProviderError(
            kind="invalid_response",
            message=(
                "Банк России подтвердил ИНН в поиске, "
                "но не вернул данные участника"
            ),
        )

    return participant


class CbrFinorgProvider:
    def __init__(self, client=None):
        self._external_client = client

    def _client(self):
        if self._external_client is not None:
            return self._external_client, False

        return (
            httpx.Client(
                timeout=httpx.Timeout(
                    REQUEST_TIMEOUT_SECONDS
                ),
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Kontragent/1.0 official-data-check"
                    ),
                },
            ),
            True,
        )

    @staticmethod
    def _envelope(body: str) -> bytes:
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}">'
            "<soap:Body>"
            f"{body}"
            "</soap:Body>"
            "</soap:Envelope>"
        )
        return xml.encode("utf-8")

    def _post(
        self,
        *,
        action: str,
        body: str,
        observations: list[dict] | None = None,
    ) -> bytes:
        client, should_close = self._client()
        request_content = self._envelope(body)

        try:
            try:
                response = client.post(
                    SERVICE_URL,
                    content=request_content,
                    headers={
                        "Content-Type": (
                            "text/xml; charset=utf-8"
                        ),
                        "SOAPAction": (
                            f'"{SOAP_ACTION_BASE}{action}"'
                        ),
                    },
                )
            except httpx.TimeoutException as error:
                raise CbrFinorgProviderError(
                    kind="timeout",
                    message=(
                        "Банк России: превышено время ожидания "
                        "веб-сервиса участников финансового рынка"
                    ),
                ) from error
            except httpx.RequestError as error:
                raise CbrFinorgProviderError(
                    kind="network_error",
                    message=(
                        "Банк России: ошибка сетевого запроса "
                        "веб-сервиса участников финансового рынка"
                    ),
                ) from error

            if response.status_code != 200:
                kind = (
                    "service_unavailable"
                    if response.status_code >= 500
                    else "http_error"
                )

                raise CbrFinorgProviderError(
                    kind=kind,
                    message=(
                        "Банк России вернул HTTP "
                        f"{response.status_code}"
                    ),
                    http_status=response.status_code,
                )

            response_content = bytes(response.content)
            if observations is not None:
                observations.append(
                    {
                        "action": action,
                        "request_content": request_content,
                        "response_content": response_content,
                        "http_status": response.status_code,
                        "response_headers": dict(
                            getattr(response, "headers", {}) or {}
                        ),
                    }
                )
            return response_content

        finally:
            if should_close:
                client.close()

    def check_inn(
        self,
        inn: str,
        *,
        _observations: list[dict] | None = None,
    ) -> dict:
        clean_inn = normalize_inn(inn)

        if len(clean_inn) not in {10, 12}:
            raise CbrFinorgProviderError(
                kind="validation_error",
                message=(
                    "Для проверки Банка России нужен ИНН "
                    "из 10 или 12 цифр"
                ),
            )

        search_xml = self._post(
            action="SearchByINNs",
            body=(
                f'<SearchByINNs xmlns="{WEB_NS}">'
                "<INNs>"
                f"<INN>{clean_inn}</INN>"
                "</INNs>"
                "</SearchByINNs>"
            ),
            observations=_observations,
        )

        search = parse_search_by_inns_response(
            search_xml,
            requested_inn=clean_inn,
        )

        if not search["found"]:
            return {
                "found": False,
                "participant": None,
                "search_record": None,
                "http_status": 200,
            }

        full_xml = self._post(
            action="GetFullInfoByINN",
            body=(
                f'<GetFullInfoByINN xmlns="{WEB_NS}">'
                f"<INN>{clean_inn}</INN>"
                "</GetFullInfoByINN>"
            ),
            observations=_observations,
        )

        participant = parse_full_info_response(
            full_xml,
            requested_inn=clean_inn,
        )

        search_ogrn = normalize_inn(
            (search.get("record") or {}).get("ogrn")
        )
        participant_ogrn = normalize_inn(
            participant.get("ogrn")
        )
        if (
            search_ogrn
            and participant_ogrn
            and search_ogrn != participant_ogrn
        ):
            raise CbrFinorgProviderError(
                kind="identity_mismatch",
                message=(
                    "Банк России вернул несовпадающий ОГРН "
                    "в поиске и полной карточке"
                ),
            )

        return {
            "found": True,
            "participant": participant,
            "search_record": search["record"],
            "http_status": 200,
        }

    def check_inn_with_raw(self, inn: str) -> dict:
        """Return the parsed lookup plus exact SOAP request/response bytes."""

        observations: list[dict] = []
        result = self.check_inn(inn, _observations=observations)
        return {**result, "observations": tuple(observations)}
