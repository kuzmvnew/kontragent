from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import roszdrav_license_worker as worker
from app.models.company import Company
from app.models.roszdrav import RoszdravLicenseEntry
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.providers.roszdrav_open_data_provider import (
    RoszdravOpenDataRelease,
    parse_open_data_release,
)
from app.services import data_readiness_scheduler as scheduler
from app.services import roszdrav_service
from scripts import run_data_readiness_scheduler as supervisor


NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
LICENSE_XML = b'''<?xml version="1.0" encoding="utf-8"?>
<licenses_list><licenses>
<name>Roszdravnadzor</name><activity_type>Pharma</activity_type>
<full_name_licensee>Test LLC</full_name_licensee><form>LLC</form>
<address>Moscow</address><ogrn>1027700000000</ogrn><inn>7701234567</inn>
<number>L-001</number><date>20.09.2026</date><date_register>20.09.2026</date_register>
</licenses></licenses_list>'''
LICENSE_XSD = b'''<?xml version="1.0" encoding="utf-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="licenses_list">
    <xs:complexType><xs:sequence>
      <xs:element name="licenses" minOccurs="1" maxOccurs="unbounded">
        <xs:complexType><xs:sequence>
          <xs:any minOccurs="0" maxOccurs="unbounded" processContents="skip"/>
        </xs:sequence></xs:complexType>
      </xs:element>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>'''


def _archive(content=LICENSE_XML):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("licenses.xml", content)
    return buffer.getvalue()


def _release(spec=worker.SPECS["pharma"]):
    base = spec.source_page_url.rstrip("/")
    return RoszdravOpenDataRelease(
        source_page_url=spec.source_page_url,
        artifact_url=f"{base}/data-20260920-structure-20210307.zip",
        xsd_url=f"{base}/structure-20210307.xsd",
        source_data_date=date(2026, 9, 20),
        actual_until=date(2026, 9, 27),
        discovered_at=NOW,
        provenance="Обновление набора данных",
    )


class FakeProvider:
    def __init__(self, release=None, *, xml=LICENSE_XML):
        self.release = release or _release()
        self.archive = _archive(xml)
        self.calls = []

    def discover(self, source_page_url, *, now=None):
        assert source_page_url == self.release.source_page_url
        return self.release

    def download(self, url):
        self.calls.append(url)
        content = LICENSE_XSD if url == self.release.xsd_url else self.archive
        return {
            "content": content,
            "headers": {"content-length": str(len(content))},
            "http_status": 200,
        }


class FakeContext:
    def __init__(self, raw_root: Path, release=None, *, check_only=False):
        self.release = release or _release()
        self.schedule_metadata = {
            **self.release.as_metadata(),
            "raw_root": str(raw_root),
            "check_only": check_only,
        }
        self.heartbeats = 0
        self.progress = []

    def ensure_active(self, *, now):
        assert now.tzinfo is not None

    def heartbeat(self):
        self.heartbeats += 1

    def report_counters(self, counters):
        self.progress.append(counters)


@pytest.fixture
def roszdrav_db():
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


def _source_and_dataset(session, code):
    source = session.scalar(
        sa.select(DataSource).where(DataSource.code == "roszdravnadzor")
    )
    if source is None:
        source = DataSource(
            code="roszdravnadzor",
            name="Росздравнадзор",
            source_type="official",
            priority=10,
            enabled=True,
        )
        session.add(source)
        session.flush()
    dataset = session.scalar(sa.select(DataSet).where(DataSet.code == code))
    if dataset is None:
        dataset = DataSet(
            source_id=source.id,
            code=code,
            name=code,
            domain="healthcare_licenses",
            update_mode="bulk",
            data_format="zip_xml",
            refresh_schedule="weekly",
            priority=10,
            enabled=False,
        )
        session.add(dataset)
        session.flush()
    return dataset


def test_official_passport_discovery_separates_source_and_actual_until_dates():
    page = b'''<html><body><table>
    <tr><td>8</td><td>Hyperlink</td><td><a href="data-20260920-structure-20210307.zip">ZIP</a></td></tr>
    <tr><td>11</td><td>Structure</td><td><a href="structure-20210307.xsd">XSD</a></td></tr>
    <tr><td>13</td><td>\xd0\x94\xd0\xb0\xd1\x82\xd0\xb0 \xd0\xbf\xd0\xbe\xd1\x81\xd0\xbb\xd0\xb5\xd0\xb4\xd0\xbd\xd0\xb5\xd0\xb3\xd0\xbe \xd0\xb2\xd0\xbd\xd0\xb5\xd1\x81\xd0\xb5\xd0\xbd\xd0\xb8\xd1\x8f \xd0\xb8\xd0\xb7\xd0\xbc\xd0\xb5\xd0\xbd\xd0\xb5\xd0\xbd\xd0\xb8\xd0\xb9</td><td>20.09.2026</td></tr>
    <tr><td>14</td><td>\xd0\xa1\xd0\xbe\xd0\xb4\xd0\xb5\xd1\x80\xd0\xb6\xd0\xb0\xd0\xbd\xd0\xb8\xd0\xb5 \xd0\xbf\xd0\xbe\xd1\x81\xd0\xbb\xd0\xb5\xd0\xb4\xd0\xbd\xd0\xb5\xd0\xb3\xd0\xbe \xd0\xb8\xd0\xb7\xd0\xbc\xd0\xb5\xd0\xbd\xd0\xb5\xd0\xbd\xd0\xb8\xd1\x8f</td><td>\xd0\x9e\xd0\xb1\xd0\xbd\xd0\xbe\xd0\xb2\xd0\xbb\xd0\xb5\xd0\xbd\xd0\xb8\xd0\xb5 \xd0\xbd\xd0\xb0\xd0\xb1\xd0\xbe\xd1\x80\xd0\xb0 \xd0\xb4\xd0\xb0\xd0\xbd\xd0\xbd\xd1\x8b\xd1\x85</td></tr>
    <tr><td>15</td><td>\xd0\x94\xd0\xb0\xd1\x82\xd0\xb0 \xd0\xb0\xd0\xba\xd1\x82\xd1\x83\xd0\xb0\xd0\xbb\xd1\x8c\xd0\xbd\xd0\xbe\xd1\x81\xd1\x82\xd0\xb8 \xd0\xbd\xd0\xb0\xd0\xb1\xd0\xbe\xd1\x80\xd0\xb0 \xd0\xb4\xd0\xb0\xd0\xbd\xd0\xbd\xd1\x8b\xd1\x85</td><td>27.09.2026</td></tr>
    </table></body></html>'''
    release = parse_open_data_release(
        page,
        source_page_url=worker.SPECS["pharma"].source_page_url,
        discovered_at=NOW,
    )
    assert release.source_data_date == date(2026, 9, 20)
    assert release.actual_until == date(2026, 9, 27)
    assert release.artifact_url.endswith("data-20260920-structure-20210307.zip")
    assert release.xsd_url.endswith("structure-20210307.xsd")


def test_handler_validates_xsd_and_persists_content_addressed_raw(tmp_path):
    provider = FakeProvider()
    context = FakeContext(tmp_path)
    result = worker.run_roszdrav_license_handler(
        context, spec=worker.SPECS["pharma"], provider=provider
    )

    assert result.counters.records_seen == 1
    assert result.counters.records_rejected == 0
    assert result.staging_result.validation.metadata["source_data_date"] == "2026-09-20"
    assert result.staging_result.validation.metadata["official_actual_until"] == "2026-09-27"
    artifact = Path(result.raw_artifacts[0].artifact_reference.removeprefix("file://"))
    assert artifact.read_bytes() == provider.archive
    assert artifact.parent.name == result.raw_artifacts[0].checksum
    assert (artifact.parent / "structure-20210307.xsd").read_bytes() == LICENSE_XSD
    assert (artifact.parent / "retrieval-manifest.json").is_file()
    assert (artifact.parent / "manifest.json").is_file()
    assert context.heartbeats == 1


def test_daily_schedules_are_idempotent_but_keep_three_distinct_sources(
    monkeypatch, tmp_path
):
    release = _release()
    provider = FakeProvider(release)
    calls = []
    monkeypatch.setattr(
        worker,
        "create_job",
        lambda _session, **kwargs: calls.append(kwargs) or kwargs,
    )
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)

    class Session:
        def get(self, model, _key):
            return approval if model is WorkerHandlerRegistration else None

    first = worker.schedule_roszdrav_license_check(
        Session(),
        category="pharma",
        raw_root=tmp_path,
        now=NOW,
        provider=provider,
    )
    second = worker.schedule_roszdrav_license_check(
        Session(),
        category="pharma",
        raw_root=tmp_path,
        now=NOW,
        provider=provider,
    )

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["source_id"] == "roszdrav_pharma_licenses"
    assert first["schedule_metadata"]["check_only"] is False
    assert scheduler.ROSZDRAV_LICENSE_DATASET_CODES == {
        "roszdrav_pharma_licenses",
        "roszdrav_narcotics_licenses",
        "roszdrav_medical_device_maintenance_licenses",
    }
    assert all(code in scheduler.HANDLERS for code in scheduler.ROSZDRAV_LICENSE_DATASET_CODES)
    assert len(calls) == 2


def test_schedule_rejects_relative_production_raw_root():
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)

    class Session:
        def get(self, model, _key):
            return approval if model is WorkerHandlerRegistration else None

    with pytest.raises(worker.InvalidDataError, match="raw_root must be absolute"):
        worker.schedule_roszdrav_license_check(
            Session(),
            category="pharma",
            raw_root=Path("relative/raw"),
            now=NOW,
            provider=FakeProvider(),
        )


def test_change_summary_preserves_multiple_facts_for_one_licence_number():
    old = [
        SimpleNamespace(
            category="pharma", inn="7701234567", license_number="L-001",
            record_key="same",
        ),
        SimpleNamespace(
            category="pharma", inn="7701234567", license_number="L-001",
            record_key="old-version",
        ),
    ]
    new = [
        {
            "category": "pharma", "inn": "7701234567",
            "license_number": "L-001", "record_key": "same",
        },
        {
            "category": "pharma", "inn": "7701234567",
            "license_number": "L-001", "record_key": "new-version",
        },
        {
            "category": "pharma", "inn": "7701234567",
            "license_number": "L-001", "record_key": "new-place",
        },
    ]

    assert worker._change_counts(old, new) == {
        "new_facts": 1,
        "changed_facts": 1,
        "removed_or_expired_facts": 0,
        "unchanged_facts": 1,
    }


def test_family_activation_ux_prepares_roszdrav_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_roszdrav_datasets",
        lambda: calls.append("roszdrav_licenses"),
    )

    supervisor.ensure_activation_datasets(
        sorted(scheduler.ROSZDRAV_LICENSE_DATASET_CODES)
    )

    assert calls == ["roszdrav_licenses"]


def test_postgresql_publish_has_exact_match_change_summary_and_same_release_check(
    roszdrav_db, tmp_path, monkeypatch
):
    suffix = str(uuid4().int % 10**8).zfill(8)
    inn = f"77{suffix}"
    xml = LICENSE_XML.replace(b"7701234567", inn.encode())
    provider = FakeProvider(xml=xml)
    context = FakeContext(tmp_path)
    result = worker.run_roszdrav_license_handler(
        context, spec=worker.SPECS["pharma"], provider=provider
    )
    monkeypatch.setattr(worker, "utc_now", lambda: NOW)

    with roszdrav_db() as session:
        dataset = _source_and_dataset(session, worker.SPECS["pharma"].dataset_code)
        session.execute(
            sa.delete(RoszdravLicenseEntry).where(
                RoszdravLicenseEntry.dataset_id == dataset.id
            )
        )
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == worker.SPECS["pharma"].source_id
            )
        )
        session.add(Company(inn=inn, name="Exact match", entity_type="legal"))
        session.flush()
        claim = SimpleNamespace(schedule_metadata=context.schedule_metadata)
        published = worker.publish_roszdrav_license_result(
            session, claim, result, spec=worker.SPECS["pharma"]
        )

        assert dataset.coverage["matching_method"] == "inn_exact"
        assert dataset.coverage["matched"] == 1
        assert dataset.coverage["matched_companies"] == 1
        assert dataset.coverage["change_summary"] == published.change_summary.as_dict()
        assert published.change_summary.new_facts == 1
        assert published.change_summary.previous_source_data_date is None
        assert published.counters.records_published == 1

        state = WorkerPublicationState(
            source_id=worker.SPECS["pharma"].source_id,
            active_pointer=published.staging_result.staging_pointer,
            generation=1,
            last_fencing_token=1,
            validation_metadata={
                "checksum": published.staging_result.checksum,
                "validation": published.staging_result.validation.metadata,
            },
        )
        session.add(state)
        session.flush()
        checked_at = NOW + timedelta(hours=1)
        monkeypatch.setattr(worker, "utc_now", lambda: checked_at)
        checked = worker.publish_roszdrav_license_result(
            session,
            claim,
            worker.HandlerResult(
                checksum_metadata={
                    "check_only": True,
                    "release_identity": provider.release.identity,
                }
            ),
            spec=worker.SPECS["pharma"],
        )
        session.flush()

        assert checked.staging_result is None
        assert checked.change_summary.unchanged_facts == 1
        assert checked.counters.records_published == 0
        assert dataset.checked_at == checked_at
        assert session.get(WorkerPublicationState, state.source_id).generation == 1
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(RoszdravLicenseEntry)
            .where(RoszdravLicenseEntry.dataset_id == dataset.id)
        ) == 1


def test_postgresql_bulk_api_requires_all_three_fresh_categories(
    roszdrav_db, monkeypatch
):
    suffix = str(uuid4().int % 10**8).zfill(8)
    inn = f"78{suffix}"
    with roszdrav_db() as session:
        datasets = {}
        for category, code in worker.LICENSE_DATASETS.items():
            dataset = _source_and_dataset(session, code)
            datasets[category] = dataset
            session.execute(
                sa.delete(RoszdravLicenseEntry).where(
                    RoszdravLicenseEntry.dataset_id == dataset.id
                )
            )
            dataset.operational_status = "current"
            dataset.last_data_date = date(2026, 9, 20)
            dataset.official_actual_until = date(2026, 9, 27)
            dataset.record_count = 1
            session.add(
                RoszdravLicenseEntry(
                    dataset_id=dataset.id,
                    data_date=date(2026, 9, 20),
                    record_key=f"{category}-{suffix}",
                    category=category,
                    inn=inn if category == "pharma" else f"79{suffix}",
                    ogrn=None,
                    license_number=f"L-{category}-{suffix}",
                    raw_payload={},
                    work_places=[],
                )
            )
        session.flush()
        monkeypatch.setattr(roszdrav_service, "get_session", roszdrav_db)

        found = roszdrav_service.get_roszdrav_bulk_license_check_for_inn(inn)
        assert found["checked"] is True
        assert found["result"] == "found"
        assert found["records"][0]["category"] == "pharma"

        datasets["narcotics"].official_actual_until = date(2026, 9, 24)
        session.flush()
        stale = roszdrav_service.get_roszdrav_bulk_license_check_for_inn(inn)
        assert stale["checked"] is False
        assert stale["result"] == "unavailable"
        assert stale["reason"] == "dataset_stale"
