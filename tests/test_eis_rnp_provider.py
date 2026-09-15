from datetime import date, datetime, timezone
import xml.etree.ElementTree as ET

from app.providers.eis_rnp_provider import (
    RNP_DOCUMENT_TYPE,
    RNP_SUBSYSTEM_TYPE,
    build_rnp_by_region_date_request,
    build_rnp_by_registry_number_request,
    parse_eis_response,
)


def _local_name(tag):
    return str(tag).split("}")[-1]


def _selection_child_names(xml_bytes):
    root = ET.fromstring(xml_bytes)

    for element in root.iter():
        if _local_name(element.tag) == "selectionParams":
            return [
                _local_name(child.tag)
                for child in list(element)
            ]

    raise AssertionError("selectionParams not found")


def test_build_rnp_registry_number_request():
    xml_bytes = (
        build_rnp_by_registry_number_request(
            token="secret-token",
            registry_number="24015080",
            request_id=(
                "11111111-2222-3333-4444-555555555555"
            ),
            created_at=datetime(
                2026,
                9,
                15,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )
    )

    text = xml_bytes.decode("utf-8")

    assert "individualPerson_token" in text
    assert "secret-token" in text
    assert "getDocsByReestrNumberRequest" in text
    assert RNP_SUBSYSTEM_TYPE in text
    assert "24015080" in text

    assert _selection_child_names(
        xml_bytes
    ) == [
        "subsystemType",
        "reestrNumber",
    ]


def test_build_rnp_region_date_request_preserves_schema_order():
    xml_bytes = (
        build_rnp_by_region_date_request(
            token="secret-token",
            region_code="77",
            exact_date=date(
                2025,
                1,
                20,
            ),
            request_id=(
                "11111111-2222-3333-4444-555555555555"
            ),
            created_at=datetime(
                2026,
                9,
                15,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )
    )

    text = xml_bytes.decode("utf-8")

    assert "getDocsByOrgRegionRequest" in text
    assert RNP_SUBSYSTEM_TYPE in text
    assert RNP_DOCUMENT_TYPE in text
    assert "2025-01-20" in text

    assert _selection_child_names(
        xml_bytes
    ) == [
        "orgRegion",
        "subsystemType",
        "documentType44",
        "periodInfo",
    ]


def test_parse_eis_archive_response():
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>
      <soap:Body>
        <response>
          <dataInfo>
            <archiveUrl>https://example.test/archive.zip</archiveUrl>
          </dataInfo>
        </response>
      </soap:Body>
    </soap:Envelope>
    """

    result = parse_eis_response(xml)

    assert result["status"] == "archive"
    assert (
        result["archive_url"]
        == "https://example.test/archive.zip"
    )


def test_parse_eis_error_response():
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>
      <soap:Body>
        <response>
          <dataInfo>
            <errorInfo>
              <code>401</code>
              <message>Access denied</message>
            </errorInfo>
          </dataInfo>
        </response>
      </soap:Body>
    </soap:Envelope>
    """

    result = parse_eis_response(xml)

    assert result["status"] == "error"
    assert result["error_code"] == "401"
    assert result["error_message"] == "Access denied"


def test_parse_eis_no_data_response():
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <soap:Envelope xmlns:soap='http://schemas.xmlsoap.org/soap/envelope/'>
      <soap:Body>
        <response>
          <dataInfo><noData>true</noData></dataInfo>
        </response>
      </soap:Body>
    </soap:Envelope>
    """

    result = parse_eis_response(xml)

    assert result["status"] == "no_data"
