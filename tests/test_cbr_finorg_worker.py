from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import cbr_finorg_worker as worker
from app.models.cbr_finorg import CbrFinorgCheck
from app.models.company import Company
from app.models.source import DataSet, DataSource
from app.providers.cbr_finorg_provider import CbrFinorgProviderError
from app.services import cbr_finorg_service


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
INN = "7707083893"
OGRN = "1027700132195"


class FakeProvider:
    def __init__(self, *, found=True, ogrn=OGRN):
        self.found = found
        self.ogrn = ogrn

    def check_inn_with_raw(self, inn):
        participant = None
        if self.found:
            participant = {
                "cbr_id": 101,
                "inn": inn,
                "ogrn": self.ogrn,
                "name": "ПАО ТЕСТ БАНК",
                "short_name": "ПАО ТЕСТ БАНК",
                "status": "Active",
                "fo_types": ["BNK"],
                "licenses": [],
                "payment_systems": [],
                "mfo_history": [],
                "websites": [],
            }
        return {
            "found": self.found,
            "participant": participant,
            "search_record": {"inn": inn, "ogrn": self.ogrn} if self.found else None,
            "http_status": 200,
            "observations": (
                {
                    "action": "SearchByINNs",
                    "request_content": b"<request />",
                    "response_content": b"<response />",
                    "http_status": 200,
                    "response_headers": {"content-type": "text/xml"},
                },
            ),
        }


class FakeContext:
    def __init__(self, raw_root: Path, *, inn=INN):
        self.schedule_metadata = {
            "source_url": worker.SERVICE_URL,
            "inn": inn,
            "request_date": NOW.date().isoformat(),
            "requested_at": NOW.isoformat(),
            "raw_root": str(raw_root),
        }
        self.progress = []

    def ensure_active(self, *, now):
        assert now.tzinfo is not None

    def heartbeat(self):
        return None

    def report_counters(self, counters):
        self.progress.append(counters)


@pytest.fixture
def worker_db():
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


def test_handler_preserves_immutable_soap_and_normalized_result(tmp_path):
    first = worker.run_cbr_finorg_handler(
        FakeContext(tmp_path), provider=FakeProvider()
    )
    second = worker.run_cbr_finorg_handler(
        FakeContext(tmp_path), provider=FakeProvider()
    )

    assert first.raw_artifacts[0].checksum == second.raw_artifacts[0].checksum
    assert first.staging_result.staging_pointer == second.staging_result.staging_pointer
    assert first.staging_result.validation.metadata == {
        "inn": INN,
        "request_date": "2026-09-25",
        "found": True,
        "matching_method": "inn_exact_with_ogrn_consistency",
    }
    artifact = Path(first.raw_artifacts[0].artifact_reference.removeprefix("file://"))
    assert artifact.is_file()
    assert (artifact.parent / "manifest.json").is_file()


def test_on_demand_enqueue_is_idempotent_and_has_no_mass_schedule(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        worker,
        "create_job",
        lambda _session, **kwargs: calls.append(kwargs) or kwargs,
    )
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)
    session = SimpleNamespace(get=lambda *_args: approval)

    first = worker.enqueue_cbr_finorg_check(
        session, inn=INN, request_date=NOW.date(), raw_root=tmp_path, now=NOW
    )
    second = worker.enqueue_cbr_finorg_check(
        session, inn=INN, request_date=NOW.date(), raw_root=tmp_path, now=NOW
    )

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["schedule_metadata"]["execution_mode"] == "on_demand"
    assert first["source_id"] == worker.SOURCE_ID
    assert len(calls) == 2


def test_provider_failures_keep_worker_retry_classification(tmp_path):
    class BrokenProvider:
        def check_inn_with_raw(self, _inn):
            raise CbrFinorgProviderError(kind="timeout", message="late")

    with pytest.raises(Exception) as captured:
        worker.run_cbr_finorg_handler(
            FakeContext(tmp_path), provider=BrokenProvider()
        )
    assert getattr(captured.value, "retryable", False) is True


def test_ordinary_legal_and_ip_are_applicable_until_exact_check(monkeypatch):
    class EmptySession:
        def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def close(self):
            return None

    monkeypatch.setattr(cbr_finorg_service, "get_session", lambda: EmptySession())
    for inn in ("7701234567", "770123456789"):
        result = cbr_finorg_service.get_cached_cbr_finorg_check_for_inn(
            inn, NOW.date()
        )
        assert result["applicable"] is True
        assert result["result"] == "unavailable"
        assert result["reason"] == "not_checked"


def test_authoritative_no_hit_is_not_applicable_and_publishes_no_fact(
    tmp_path, monkeypatch
):
    result = worker.run_cbr_finorg_handler(
        FakeContext(tmp_path), provider=FakeProvider(found=False)
    )
    assert result.staging_result.validation.metadata["found"] is False

    row = SimpleNamespace(
        is_participant=False,
        licenses=[],
        fo_types=[],
        payment_systems=[],
        mfo_history=[],
        status=None,
        cbr_id=None,
        ogrn=None,
        name=None,
        short_name=None,
        regnum=None,
        bic=None,
        registration_date=None,
        checked_at=NOW,
        request_date=NOW.date(),
    )
    projected = cbr_finorg_service._serialize_success(row, cached=True)
    assert projected["checked"] is True
    assert projected["applicable"] is False
    assert projected["result"] == "not_applicable"
    assert projected["reason"] == "official_exact_inn_no_participant"


def test_postgresql_atomic_idempotent_cache_and_exact_ogrn(worker_db, tmp_path, monkeypatch):
    suffix = str(uuid4().int % 10**8).zfill(8)
    inn = f"77{suffix}"
    ogrn = f"1{str(uuid4().int % 10**12).zfill(12)}"
    monkeypatch.setattr(worker, "utc_now", lambda: NOW)
    result = worker.run_cbr_finorg_handler(
        FakeContext(tmp_path, inn=inn), provider=FakeProvider(ogrn=ogrn)
    )

    with worker_db() as session:
        source = session.scalar(sa.select(DataSource).where(DataSource.code == "cbr"))
        if source is None:
            source = DataSource(
                code="cbr", name="CBR", source_type="official", priority=10, enabled=True
            )
            session.add(source)
            session.flush()
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == worker.DATASET_CODE)
        )
        if dataset is None:
            dataset = DataSet(
                source_id=source.id,
                code=worker.DATASET_CODE,
                name="CBR FINORG",
                domain="financial_market_participants",
                update_mode="api",
                data_format="soap_xml",
                priority=10,
                enabled=False,
            )
            session.add(dataset)
            session.flush()
        session.execute(
            sa.delete(CbrFinorgCheck).where(CbrFinorgCheck.dataset_id == dataset.id)
        )
        session.add(Company(inn=inn, ogrn=ogrn, name="Exact", entity_type="legal"))
        session.flush()

        claim = SimpleNamespace(schedule_metadata=FakeContext(tmp_path, inn=inn).schedule_metadata)
        first = worker.publish_cbr_finorg_worker_result(session, claim, result)
        second = worker.publish_cbr_finorg_worker_result(session, claim, result)

        assert first.change_summary.new_facts == 1
        assert first.change_summary.matched_companies == 1
        assert second.change_summary.unchanged_facts == 1
        assert second.counters.records_published == 0
        assert dataset.coverage["change_summary"]["unchanged_facts"] == 1
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CbrFinorgCheck).where(
                CbrFinorgCheck.dataset_id == dataset.id,
                CbrFinorgCheck.inn == inn,
            )
        ) == 1

        mismatch = worker.run_cbr_finorg_handler(
            FakeContext(tmp_path / "mismatch", inn=inn),
            provider=FakeProvider(ogrn="2" + ogrn[1:]),
        )
        with pytest.raises(Exception, match="differs from Master"):
            worker.publish_cbr_finorg_worker_result(session, claim, mismatch)
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CbrFinorgCheck).where(
                CbrFinorgCheck.dataset_id == dataset.id,
                CbrFinorgCheck.inn == inn,
            )
        ) == 1
