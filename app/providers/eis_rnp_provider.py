import hashlib
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv()


EIS_IP_ENDPOINT = (
    "https://int44.zakupki.gov.ru/"
    "eis-integration/services/getDocsIP"
)

SOAP_NS = (
    "http://schemas.xmlsoap.org/soap/envelope/"
)

WS_NS = (
    "http://zakupki.gov.ru/fz44/get-docs-ip/ws"
)

RNP_SUBSYSTEM_TYPE = "RNP"
RNP_DOCUMENT_TYPE = "unfairSupplier2022"
TOKEN_ENV_NAME = "EIS_IP_TOKEN"


class EisCredentialError(RuntimeError):
    pass


class EisResponseError(RuntimeError):
    pass


def get_eis_ip_token():
    token = (
        os.getenv(TOKEN_ENV_NAME)
        or ""
    ).strip()

    if not token:
        raise EisCredentialError(
            "Не найден EIS_IP_TOKEN. "
            "Сначала зарегистрируй получателя "
            "машиночитаемых данных в ЕИС и "
            "сохрани token только локально."
        )

    return token


def _format_created_at(value=None):
    current = (
        value
        or datetime.now(timezone.utc)
    )

    if current.tzinfo is None:
        current = current.replace(
            tzinfo=timezone.utc
        )

    return (
        current
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _soap_root(token):
    ET.register_namespace(
        "soapenv",
        SOAP_NS,
    )
    ET.register_namespace(
        "ws",
        WS_NS,
    )

    envelope = ET.Element(
        f"{{{SOAP_NS}}}Envelope"
    )

    header = ET.SubElement(
        envelope,
        f"{{{SOAP_NS}}}Header",
    )

    token_node = ET.SubElement(
        header,
        "individualPerson_token",
    )
    token_node.text = token

    body = ET.SubElement(
        envelope,
        f"{{{SOAP_NS}}}Body",
    )

    return envelope, body


def _add_index(
    parent,
    request_id=None,
    created_at=None,
):
    index = ET.SubElement(
        parent,
        "index",
    )

    id_node = ET.SubElement(
        index,
        "id",
    )
    id_node.text = (
        request_id
        or str(uuid.uuid4())
    )

    created_node = ET.SubElement(
        index,
        "createDateTime",
    )
    created_node.text = (
        _format_created_at(
            created_at
        )
    )

    mode = ET.SubElement(
        index,
        "mode",
    )
    mode.text = "PROD"


def build_rnp_by_registry_number_request(
    *,
    token,
    registry_number,
    request_id=None,
    created_at=None,
):
    registry_number = str(
        registry_number
        or ""
    ).strip()

    if not registry_number:
        raise ValueError(
            "registry_number обязателен"
        )

    envelope, body = (
        _soap_root(token)
    )

    request = ET.SubElement(
        body,
        f"{{{WS_NS}}}getDocsByReestrNumberRequest",
    )

    _add_index(
        request,
        request_id=request_id,
        created_at=created_at,
    )

    selection = ET.SubElement(
        request,
        "selectionParams",
    )

    subsystem = ET.SubElement(
        selection,
        "subsystemType",
    )
    subsystem.text = (
        RNP_SUBSYSTEM_TYPE
    )

    registry = ET.SubElement(
        selection,
        "reestrNumber",
    )
    registry.text = registry_number

    return ET.tostring(
        envelope,
        encoding="utf-8",
        xml_declaration=True,
    )


def build_rnp_by_region_date_request(
    *,
    token,
    region_code,
    exact_date,
    request_id=None,
    created_at=None,
):
    region_code = str(
        region_code
        or ""
    ).strip()

    if not region_code:
        raise ValueError(
            "region_code обязателен"
        )

    if isinstance(
        exact_date,
        date,
    ):
        date_value = (
            exact_date.isoformat()
        )
    else:
        date_value = str(
            exact_date
            or ""
        ).strip()

    if not date_value:
        raise ValueError(
            "exact_date обязателен"
        )

    envelope, body = (
        _soap_root(token)
    )

    request = ET.SubElement(
        body,
        f"{{{WS_NS}}}getDocsByOrgRegionRequest",
    )

    _add_index(
        request,
        request_id=request_id,
        created_at=created_at,
    )

    selection = ET.SubElement(
        request,
        "selectionParams",
    )

    region = ET.SubElement(
        selection,
        "orgRegion",
    )
    region.text = region_code

    subsystem = ET.SubElement(
        selection,
        "subsystemType",
    )
    subsystem.text = (
        RNP_SUBSYSTEM_TYPE
    )

    document_type = ET.SubElement(
        selection,
        "documentType44",
    )
    document_type.text = (
        RNP_DOCUMENT_TYPE
    )

    period = ET.SubElement(
        selection,
        "periodInfo",
    )

    exact = ET.SubElement(
        period,
        "exactDate",
    )
    exact.text = date_value

    return ET.tostring(
        envelope,
        encoding="utf-8",
        xml_declaration=True,
    )


def _local_name(tag):
    return str(tag).split("}")[-1]


def _first_text_by_local_name(
    root,
    local_name,
):
    for element in root.iter():
        if (
            _local_name(element.tag)
            == local_name
        ):
            text = (
                element.text
                or ""
            ).strip()
            if text:
                return text

    return None


def parse_eis_response(xml_content):
    if isinstance(
        xml_content,
        str,
    ):
        raw = xml_content.encode(
            "utf-8"
        )
    else:
        raw = xml_content

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise EisResponseError(
            "ЕИС вернул некорректный XML"
        ) from error

    archive_url = (
        _first_text_by_local_name(
            root,
            "archiveUrl",
        )
    )

    if archive_url:
        return {
            "status": "archive",
            "archive_url": archive_url,
            "error_code": None,
            "error_message": None,
        }

    error_code = (
        _first_text_by_local_name(
            root,
            "code",
        )
    )
    error_message = (
        _first_text_by_local_name(
            root,
            "message",
        )
    )

    if (
        error_code
        or error_message
    ):
        return {
            "status": "error",
            "archive_url": None,
            "error_code": error_code,
            "error_message": (
                error_message
            ),
        }

    raw_text = raw.decode(
        "utf-8",
        errors="ignore",
    ).lower()

    if (
        "nodata" in raw_text
        or "no data" in raw_text
        or "данные отсутствуют"
        in raw_text
    ):
        return {
            "status": "no_data",
            "archive_url": None,
            "error_code": None,
            "error_message": None,
        }

    return {
        "status": "unknown",
        "archive_url": None,
        "error_code": None,
        "error_message": None,
    }


def request_archive(
    *,
    soap_xml,
    token,
    endpoint=EIS_IP_ENDPOINT,
    timeout=60,
    client=None,
):
    owns_client = client is None

    if client is None:
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
        )

    try:
        response = client.post(
            endpoint,
            content=soap_xml,
            headers={
                "Content-Type": (
                    "text/xml; charset=utf-8"
                ),
            },
        )

        response.raise_for_status()

        parsed = parse_eis_response(
            response.content
        )

        parsed[
            "http_status"
        ] = response.status_code

        return parsed

    finally:
        if owns_client:
            client.close()


def download_archive(
    *,
    archive_url,
    token,
    destination,
    timeout=180,
    client=None,
):
    destination = Path(
        destination
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    owns_client = client is None

    if client is None:
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
        )

    try:
        with client.stream(
            "GET",
            archive_url,
            headers={
                "individualPerson_token": (
                    token
                ),
            },
        ) as response:
            response.raise_for_status()

            hasher = hashlib.sha256()
            size = 0

            with destination.open(
                "wb"
            ) as file:
                for chunk in (
                    response.iter_bytes()
                ):
                    if not chunk:
                        continue

                    file.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)

        return {
            "path": str(destination),
            "size": size,
            "sha256": (
                hasher.hexdigest()
            ),
        }

    finally:
        if owns_client:
            client.close()
