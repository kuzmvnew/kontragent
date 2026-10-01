from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from http.client import IncompleteRead
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import roskomnadzor_bulk_worker as worker
from app.models.roskomnadzor import (
    RoskomnadzorCompanyFact,
    RoskomnadzorPrivatePersonRecord,
)
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerPublicationState
from app.worker.contracts import HandlerResult, StagingResult, ValidationResult
from app.worker.errors import InvalidDataError, LeaseLostError, WorkerNetworkError
from app.worker.execution import (
    claim_next_job,
    complete_run_success,
    create_job,
    register_handler,
)
from app.worker.registry import HandlerRegistry


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<root><license><inn>7707083893</inn><ogrn>1027700132195</ogrn>
<licenseNumber>L-1</licenseNumber><organizationName>TEST</organizationName>
<status>active</status></license></root>"""
MEDIA_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<root><media><name>TEST MEDIA</name><founder><inn>7707083893</inn>
<name>TEST</name></founder></media></root>"""
PERSON_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<root><license><inn>660000000000</inn><ogrn>306660000000000</ogrn>
<licenseNumber>PRIVATE-1</licenseNumber><organizationName>PERSON</organizationName>
</license></root>"""
XSD = b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'


class Context:
    def __init__(
        self,
        raw_root,
        *,
        source_id="rkn_communications_licenses",
        source_data_date="2026-09-25",
        release_identity="release-1",
        artifact_url=None,
        xsd_url="https://rkn.gov.ru/opendata/test/structure-20220708T0000.xsd",
    ):
        self.source_id = source_id
        self.schedule_metadata = {
            "artifact_url": artifact_url
            or (
                "https://rkn.gov.ru/opendata/test/"
                "data-20260925T0000-structure-20220708T0000.xml"
            ),
            "xsd_url": xsd_url,
            "source_data_date": source_data_date,
            "release_identity": release_identity,
            "raw_root": str(raw_root),
            "check_only": False,
        }
        self.progress = []

    def report_counters(self, counters):
        self.progress.append(counters)

    def heartbeat(self):
        return None


@pytest.fixture
def rkn_db():
    connection = engine.connect()
    transaction = connection.begin()
    assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def _fetch_payloads(monkeypatch, *, xml=XML, xsd=XSD):
    def fetch(url):
        content = xsd if url.endswith(".xsd") else xml
        return content, {
            "content-type": "application/xml",
            "content-length": str(len(content)),
        }

    monkeypatch.setattr(worker, "_fetch", fetch)


def _path(uri):
    return Path(uri.removeprefix("file://"))


def _validation(*, release_identity="release-new", source_data_date="2026-09-26"):
    return {
        "source_records": 1,
        "imported_records": 1,
        "duplicate_records": 0,
        "rejected_records": 0,
        "public_records": 1,
        "private_records": 0,
        "artifact_sha256": "a" * 64,
        "xsd_sha256": "b" * 64,
        "normalized_sha256": "c" * 64,
        "normalized_identity": "d" * 64,
        "source_data_date": source_data_date,
        "release_identity": release_identity,
    }


def test_official_listing_discovery_selects_newest_xml_and_xsd(monkeypatch):
    html = b"""
    <a href="data-20260924T0000-structure-20220708T0000.xml">old</a>
    <a href="data-20260925T0000-structure-20220708T0000.xml">new</a>
    <a href="structure-20220708T0000.xsd">xsd</a>
    """
    monkeypatch.setattr(
        worker, "_fetch", lambda _url: (html, {"content-type": "text/html"})
    )
    release = worker.discover_release(
        worker.SPECS["rkn_communications_licenses"]
    )
    assert release["source_data_date"] == "2026-09-25"
    assert "data-20260925" in release["artifact_url"]
    assert release["xsd_url"].endswith("structure-20220708T0000.xsd")


def test_handler_freezes_xml_xsd_and_normalized_snapshot(monkeypatch, tmp_path):
    _fetch_payloads(monkeypatch)
    result = worker.run_rkn_bulk_handler(Context(tmp_path))
    metadata = result.staging_result.validation.metadata

    assert metadata["source_records"] == 1
    assert metadata["public_records"] == 1
    assert metadata["private_records"] == 0
    assert metadata["xsd_sha256"]
    assert metadata["normalized_identity"]
    assert metadata["parser_contract"]["version"] == (
        worker.NORMALIZATION_CONTRACT_VERSION
    )
    assert len(result.raw_artifacts) == 2
    assert result.counters.records_written == 1


def test_identical_xml_acquisitions_keep_first_manifest_and_reuse_release(
    monkeypatch, tmp_path
):
    _fetch_payloads(monkeypatch)
    clock = iter((NOW, NOW + timedelta(hours=1)))
    monkeypatch.setattr(worker, "utc_now", lambda: next(clock))

    first = worker.run_rkn_bulk_handler(Context(tmp_path))
    artifact = _path(first.raw_artifacts[0].artifact_reference)
    manifest_path = artifact.parent / "manifest.json"
    first_manifest = manifest_path.read_bytes()
    second = worker.run_rkn_bulk_handler(Context(tmp_path))

    assert manifest_path.read_bytes() == first_manifest
    assert first.raw_artifacts[0].artifact_reference == (
        second.raw_artifacts[0].artifact_reference
    )
    assert first.raw_artifacts[0].manifest["retrieved_at"] != (
        second.raw_artifacts[0].manifest["retrieved_at"]
    )
    assert first.staging_result.staging_pointer == (
        second.staging_result.staging_pointer
    )
    assert first.staging_result.checksum == second.staging_result.checksum
    assert first.checksum_metadata["normalized_identity"] == (
        second.checksum_metadata["normalized_identity"]
    )


def test_same_media_xml_on_two_dates_has_distinct_normalized_identity(
    monkeypatch, tmp_path
):
    _fetch_payloads(monkeypatch, xml=MEDIA_XML)
    first = worker.run_rkn_bulk_handler(
        Context(
            tmp_path,
            source_id="rkn_registered_media",
            source_data_date="2026-09-25",
            release_identity="media-release-1",
        )
    )
    second = worker.run_rkn_bulk_handler(
        Context(
            tmp_path,
            source_id="rkn_registered_media",
            source_data_date="2026-09-26",
            release_identity="media-release-2",
        )
    )

    assert first.raw_artifacts[0].artifact_reference == (
        second.raw_artifacts[0].artifact_reference
    )
    assert first.staging_result.staging_pointer != (
        second.staging_result.staging_pointer
    )
    assert first.checksum_metadata["normalized_identity"] != (
        second.checksum_metadata["normalized_identity"]
    )
    assert first.staging_result.checksum != second.staging_result.checksum
    assert _path(first.staging_result.staging_pointer).is_file()
    assert _path(second.staging_result.staging_pointer).is_file()


def test_parser_contract_change_has_distinct_normalized_identity(
    monkeypatch, tmp_path
):
    _fetch_payloads(monkeypatch)
    first = worker.run_rkn_bulk_handler(Context(tmp_path))
    monkeypatch.setattr(
        worker, "NORMALIZATION_CONTRACT_VERSION", "roskomnadzor-normalized-v2"
    )
    second = worker.run_rkn_bulk_handler(Context(tmp_path))

    assert first.staging_result.checksum == second.staging_result.checksum
    assert first.staging_result.staging_pointer != (
        second.staging_result.staging_pointer
    )
    assert first.checksum_metadata["normalized_identity"] != (
        second.checksum_metadata["normalized_identity"]
    )


def test_two_releases_reuse_identical_xsd_without_manifest_collision(
    monkeypatch, tmp_path
):
    _fetch_payloads(monkeypatch)
    first = worker.run_rkn_bulk_handler(
        Context(
            tmp_path,
            source_id="rkn_broadcast_licenses",
            source_data_date="2026-09-25",
            release_identity="broadcast-release-1",
            xsd_url="https://rkn.gov.ru/opendata/test/structure-1.xsd",
        )
    )
    xsd_path = _path(first.raw_artifacts[1].artifact_reference)
    first_manifest = (xsd_path.parent / "manifest.json").read_bytes()
    second = worker.run_rkn_bulk_handler(
        Context(
            tmp_path,
            source_id="rkn_broadcast_licenses",
            source_data_date="2026-09-26",
            release_identity="broadcast-release-2",
            xsd_url="https://rkn.gov.ru/opendata/test/structure-2.xsd",
        )
    )

    assert second.raw_artifacts[1].artifact_reference == (
        first.raw_artifacts[1].artifact_reference
    )
    assert (xsd_path.parent / "manifest.json").read_bytes() == first_manifest
    assert second.raw_artifacts[1].manifest["source_url"].endswith(
        "structure-2.xsd"
    )


def test_existing_immutable_content_corruption_is_rejected(monkeypatch, tmp_path):
    _fetch_payloads(monkeypatch)
    first = worker.run_rkn_bulk_handler(Context(tmp_path))
    artifact = _path(first.raw_artifacts[0].artifact_reference)
    artifact.chmod(0o644)
    artifact.write_bytes(b"corrupt")

    with pytest.raises(InvalidDataError, match="immutable Roskomnadzor artifact"):
        worker.run_rkn_bulk_handler(Context(tmp_path))


def test_existing_immutable_normalized_corruption_is_rejected(
    monkeypatch, tmp_path
):
    _fetch_payloads(monkeypatch)
    first = worker.run_rkn_bulk_handler(Context(tmp_path))
    normalized = _path(first.staging_result.staging_pointer)
    normalized.chmod(0o644)
    normalized.write_bytes(b"corrupt\n")

    with pytest.raises(
        InvalidDataError, match="immutable Roskomnadzor normalized snapshot"
    ):
        worker.run_rkn_bulk_handler(Context(tmp_path))


def test_existing_legacy_manifest_and_normalized_pointer_remain_readable(
    monkeypatch, tmp_path
):
    _fetch_payloads(monkeypatch)
    artifact_sha = sha256(XML).hexdigest()
    artifact_dir = tmp_path / "rkn_communications_licenses" / artifact_sha
    artifact_dir.mkdir(parents=True)
    legacy_manifest = {
        "source_id": "rkn_communications_licenses",
        "source_owner": "Роскомнадзор",
        "source_url": "https://rkn.gov.ru/opendata/legacy.xml",
        "source_data_date": "2026-09-01",
        "release_identity": "legacy-release",
        "sha256": artifact_sha,
        "size": len(XML),
        "retrieved_at": "2026-09-01T00:00:00+00:00",
        "immutable": True,
    }
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(legacy_manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = manifest_path.read_bytes()
    result = worker.run_rkn_bulk_handler(Context(tmp_path))

    assert manifest_path.read_bytes() == before
    assert result.raw_artifacts[0].manifest["content_manifest"] == legacy_manifest

    legacy_pointer = artifact_dir / "normalized.jsonl"
    legacy_row = {"kind": "public", "inn": "7707083893"}
    legacy_pointer.write_text(json.dumps(legacy_row) + "\n", encoding="utf-8")
    assert list(worker._iter_jsonl(legacy_pointer.as_uri())) == [legacy_row]


def test_rkn_privacy_boundary_remains_private_and_unpublished(monkeypatch, tmp_path):
    _fetch_payloads(monkeypatch, xml=PERSON_XML)
    result = worker.run_rkn_bulk_handler(Context(tmp_path))
    validation = result.staging_result.validation.metadata
    rows = list(worker._iter_jsonl(result.staging_result.staging_pointer))

    assert validation["public_records"] == 0
    assert validation["private_records"] == 1
    assert rows[0]["kind"] == "private"
    assert rows[0]["is_published"] is False
    assert rows[0]["identifier_hash"] != "660000000000"
    assert "inn" not in rows[0]


def test_failed_new_publication_preserves_prior_facts_and_pointer(
    rkn_db, monkeypatch, tmp_path
):
    source_id = "rkn_communications_licenses"
    old_pointer = tmp_path / "normalized.jsonl"
    old_pointer.write_text(
        json.dumps({"kind": "public", "inn": "7707083893"}) + "\n",
        encoding="utf-8",
    )
    old_record_key = "1" * 64

    with rkn_db() as session:
        source = session.scalar(sa.select(DataSource).where(DataSource.code == "rkn"))
        if source is None:
            source = DataSource(
                code="rkn",
                name="Роскомнадзор",
                source_type="official",
                priority=10,
                enabled=True,
            )
            session.add(source)
            session.flush()
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == source_id)
        )
        if dataset is None:
            dataset = DataSet(
                source_id=source.id,
                code=source_id,
                name=source_id,
                domain="communications",
                update_mode="bulk",
                data_format="xml",
                priority=10,
                enabled=True,
            )
            session.add(dataset)
            session.flush()
        session.execute(
            sa.delete(RoskomnadzorCompanyFact).where(
                RoskomnadzorCompanyFact.dataset_id == dataset.id
            )
        )
        session.execute(
            sa.delete(RoskomnadzorPrivatePersonRecord).where(
                RoskomnadzorPrivatePersonRecord.dataset_id == dataset.id
            )
        )
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == source_id
            )
        )
        session.add(
            RoskomnadzorCompanyFact(
                dataset_id=dataset.id,
                data_date=date(2026, 9, 25),
                record_key=old_record_key,
                channel="communications",
                inn="7707083893",
                external_number="OLD-1",
                name="ACCEPTED",
                public_details={},
            )
        )
        session.add(
            WorkerPublicationState(
                source_id=source_id,
                active_pointer=old_pointer.as_uri(),
                rollback_pointer=None,
                generation=7,
                last_fencing_token=9,
                validation_metadata={
                    "checksum": "e" * 64,
                    "validation": {
                        "release_identity": "release-old",
                        "source_data_date": "2026-09-25",
                    },
                },
            )
        )
        session.commit()

    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text("{}\n", encoding="utf-8")
    result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer=candidate.as_uri(),
            checksum="c" * 64,
            validation=ValidationResult(accepted=True, metadata=_validation()),
        )
    )

    def fail_after_delete(_pointer):
        raise InvalidDataError("candidate publication failed")
        yield  # pragma: no cover

    monkeypatch.setattr(worker, "_iter_jsonl", fail_after_delete)
    with rkn_db() as session:
        with pytest.raises(InvalidDataError, match="candidate publication failed"):
            worker.publish_rkn_bulk_result(
                session,
                SimpleNamespace(
                    source_id=source_id,
                    schedule_metadata={"release_identity": "release-new"},
                ),
                result,
            )
        session.rollback()

    with rkn_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        dataset = session.scalar(sa.select(DataSet).where(DataSet.code == source_id))
        records = list(
            session.scalars(
                sa.select(RoskomnadzorCompanyFact).where(
                    RoskomnadzorCompanyFact.dataset_id == dataset.id
                )
            )
        )
        assert state.active_pointer == old_pointer.as_uri()
        assert state.generation == 7
        assert [record.record_key for record in records] == [old_record_key]


def test_rkn_staging_pointer_still_honors_publication_fencing(rkn_db, tmp_path):
    source_id = f"rkn_fencing_{uuid4().hex}"
    registry = HandlerRegistry()
    staging_path = tmp_path / "normalized.jsonl"
    staging_path.write_text("{}\n", encoding="utf-8")

    with rkn_db() as session:
        register_handler(
            session,
            registry,
            source_id=source_id,
            version="rkn-fencing-test-v1",
            handler=lambda _context: HandlerResult(),
            approved=True,
            live=False,
            fixture=True,
        )
        create_job(
            session,
            source_id=source_id,
            job_type="rkn_fencing_test",
            handler_version="rkn-fencing-test-v1",
            idempotency_key=source_id,
            now=NOW,
        )
        session.commit()

    with rkn_db() as session:
        claim = claim_next_job(
            session,
            registry,
            worker_id="rkn-fencing-worker",
            lease_ttl=timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )
        session.commit()

    with rkn_db() as session:
        session.add(
            WorkerPublicationState(
                source_id=source_id,
                active_pointer="file:///accepted/normalized.jsonl",
                rollback_pointer=None,
                generation=3,
                last_fencing_token=claim.fencing_token + 1,
                validation_metadata={"accepted": True},
            )
        )
        session.commit()

    result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer=staging_path.as_uri(),
            checksum="f" * 64,
            validation=ValidationResult(accepted=True),
        )
    )
    with rkn_db() as session:
        with pytest.raises(LeaseLostError, match="newer fencing token"):
            complete_run_success(
                session,
                claim,
                result,
                now=NOW + timedelta(seconds=2),
            )
        session.rollback()

    with rkn_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == "file:///accepted/normalized.jsonl"
        assert state.generation == 3


def test_same_release_check_only_never_redownloads(monkeypatch, tmp_path):
    context = Context(tmp_path)
    context.schedule_metadata["check_only"] = True
    monkeypatch.setattr(
        worker,
        "_fetch",
        lambda _url: (_ for _ in ()).throw(AssertionError("network")),
    )
    result = worker.run_rkn_bulk_handler(context)
    assert result.staging_result is None
    assert result.checksum_metadata["check_only"] is True


def test_incomplete_http_read_is_retryable_network_failure(monkeypatch):
    class BrokenResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            raise IncompleteRead(b"partial")

    monkeypatch.setattr(worker, "urlopen", lambda *_args, **_kwargs: BrokenResponse())

    with pytest.raises(WorkerNetworkError) as captured:
        worker._fetch("https://rkn.gov.ru/opendata/test/data.xml")

    assert captured.value.retryable is True
