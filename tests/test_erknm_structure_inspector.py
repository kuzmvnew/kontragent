from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from scripts.inspect_erknm_open_data import (
    inspect_path,
    inspect_xml_stream,
    inspect_xsd_stream,
    local_name,
)


def test_local_name_strips_namespace():
    assert local_name("{urn:test}inspection") == "inspection"
    assert local_name("ns:inn") == "inn"
    assert local_name("status") == "status"


def test_inspect_xml_stream_counts_paths_and_samples():
    xml = b"""
    <root xmlns="urn:test">
        <inspection id="A1">
            <inn>7701234567</inn>
            <status>DONE</status>
        </inspection>
        <inspection id="A2">
            <inn>7801234567</inn>
            <status>PLANNED</status>
        </inspection>
    </root>
    """

    report = inspect_xml_stream(
        BytesIO(xml),
        max_elements=100,
        sample_values=2,
    )

    assert report["root_tag"] == "root"
    assert report["path_counts"]["root/inspection"] == 2
    assert report["path_counts"]["root/inspection/inn"] == 2
    assert report["attribute_counts"]["root/inspection/@id"] == 2
    assert report["value_samples"]["root/inspection/inn"] == [
        "7701234567",
        "7801234567",
    ]


def test_inspect_zip_selects_largest_xml(tmp_path: Path):
    archive_path = tmp_path / "dataset.zip"

    with ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "passport.xml",
            "<passport><name>ERKNM</name></passport>",
        )
        archive.writestr(
            "data.xml",
            """
            <root>
                <inspection><inn>7701234567</inn></inspection>
                <inspection><inn>7801234567</inn></inspection>
                <inspection><inn>5401234567</inn></inspection>
            </root>
            """,
        )

    report = inspect_path(
        archive_path,
        max_elements=100,
    )

    assert report["kind"] == "zip"
    assert report["selected_member"] == "data.xml"
    assert report["xml"]["root_tag"] == "root"
    assert (
        report["xml"]["path_counts"]["root/inspection/inn"]
        == 3
    )


def test_inspect_xsd_stream_lists_declared_types():
    xsd = b"""
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
        <xs:element name="root" type="RootType"/>
        <xs:complexType name="RootType">
            <xs:sequence>
                <xs:element name="inspection" type="InspectionType"/>
            </xs:sequence>
        </xs:complexType>
        <xs:complexType name="InspectionType">
            <xs:sequence>
                <xs:element name="inn" type="xs:string"/>
            </xs:sequence>
        </xs:complexType>
        <xs:simpleType name="StatusType">
            <xs:restriction base="xs:string"/>
        </xs:simpleType>
    </xs:schema>
    """

    report = inspect_xsd_stream(
        BytesIO(xsd)
    )

    assert "root" in report["elements"]
    assert "inspection" in report["elements"]
    assert "inn" in report["elements"]
    assert "RootType" in report["complex_types"]
    assert "InspectionType" in report["complex_types"]
    assert "StatusType" in report["simple_types"]
