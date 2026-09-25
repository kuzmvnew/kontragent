from datetime import datetime, timezone

from app.ingestion import roskomnadzor_bulk_worker as worker


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


class Context:
    source_id = "rkn_communications_licenses"

    def __init__(self, raw_root):
        self.schedule_metadata = {
            "artifact_url": "https://rkn.gov.ru/opendata/test/data-20260925T0000-structure-20220708T0000.xml",
            "xsd_url": "https://rkn.gov.ru/opendata/test/structure-20220708T0000.xsd",
            "source_data_date": "2026-09-25",
            "release_identity": "release-1",
            "raw_root": str(raw_root),
            "check_only": False,
        }
        self.progress = []

    def report_counters(self, counters):
        self.progress.append(counters)

    def heartbeat(self):
        return None


def test_official_listing_discovery_selects_newest_xml_and_xsd(monkeypatch):
    html = b"""
    <a href="data-20260924T0000-structure-20220708T0000.xml">old</a>
    <a href="data-20260925T0000-structure-20220708T0000.xml">new</a>
    <a href="structure-20220708T0000.xsd">xsd</a>
    """
    monkeypatch.setattr(worker, "_fetch", lambda _url: (html, {"content-type": "text/html"}))
    release = worker.discover_release(worker.SPECS["rkn_communications_licenses"])
    assert release["source_data_date"] == "2026-09-25"
    assert "data-20260925" in release["artifact_url"]
    assert release["xsd_url"].endswith("structure-20220708T0000.xsd")


def test_handler_freezes_xml_xsd_and_normalized_snapshot(monkeypatch, tmp_path):
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <root><license><inn>7707083893</inn><ogrn>1027700132195</ogrn>
    <licenseNumber>L-1</licenseNumber><organizationName>TEST</organizationName>
    <status>active</status></license></root>"""
    xsd = b"<xs:schema xmlns:xs=\"http://www.w3.org/2001/XMLSchema\"/>"

    def fetch(url):
        return (xsd, {"content-type": "application/xml"}) if url.endswith(".xsd") else (xml, {"content-type": "application/xml"})

    monkeypatch.setattr(worker, "_fetch", fetch)
    result = worker.run_rkn_bulk_handler(Context(tmp_path))
    metadata = result.staging_result.validation.metadata
    assert metadata["source_records"] == 1
    assert metadata["public_records"] == 1
    assert metadata["private_records"] == 0
    assert metadata["xsd_sha256"]
    assert len(result.raw_artifacts) == 2
    assert result.counters.records_written == 1


def test_same_release_check_only_never_redownloads(monkeypatch, tmp_path):
    context = Context(tmp_path)
    context.schedule_metadata["check_only"] = True
    monkeypatch.setattr(worker, "_fetch", lambda _url: (_ for _ in ()).throw(AssertionError("network")))
    result = worker.run_rkn_bulk_handler(context)
    assert result.staging_result is None
    assert result.checksum_metadata["check_only"] is True
