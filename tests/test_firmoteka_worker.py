from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import firmoteka_worker as worker
from app.ingestion.firmoteka_parser import parse_firmoteka_page
from app.models.company import Company
from app.models.firmoteka import FirmotekaCrawlRun
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerHandlerRegistration
from app.services.source_factory_registry_service import ensure_source_factory_datasets


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
INN = "7707083893"


def _factory():
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return connection, transaction, factory


def _page(inn=INN, name="ПАО ТЕСТ"):
    return f"""
    <html><head><script type="application/ld+json">
    {{"@type":"Organization","name":"{name}","legalName":"{name}",
      "taxID":"{inn}","identifier":{{"value":"1027700132195"}},
      "foundingDate":"2002-08-13",
      "address":{{"streetAddress":"Москва","addressRegion":"77"}}}}
    </script></head><body><h1>{name}</h1>Действующая организация ИНН {inn}</body></html>
    """.encode()


def test_parser_requires_exact_rendered_inn_and_keeps_bridge_provenance():
    parsed = parse_firmoteka_page(
        _page(), requested_inn=INN, url=f"https://firmoteka.ru/{INN}", fetched_at=NOW
    )
    assert parsed["identity_match"] is True
    assert parsed["name"] == "ПАО ТЕСТ"
    assert parsed["status_normalized"] == "ACTIVE"
    assert parsed["provenance"] == "AUTHORIZED_BRIDGE"
    mismatch = parse_firmoteka_page(
        _page("7812014560"), requested_inn=INN,
        url=f"https://firmoteka.ru/{INN}", fetched_at=NOW,
    )
    assert mismatch["identity_match"] is False


def test_catalog_extraction_is_same_origin_exact_inn_and_rejects_api():
    html = (
        f'<a href="/{INN}">ok</a>'
        '<a href="/api/company/7707083893">api</a>'
        '<a href="https://example.com/7812014560">foreign</a>'
        '<a href="/not-an-inn">other</a>'
    ).encode()
    assert worker._extract_company_links(html, worker.BASE_URL) == (
        (INN, f"https://firmoteka.ru/{INN}"),
    )
    assert worker._allowed_public_url("https://firmoteka.ru/api/company") is False


def test_content_addressed_raw_is_immutable_and_deduplicated(tmp_path):
    first, manifest = worker._store_raw(
        raw_root=tmp_path, content=_page(), kind="company",
        url=f"https://firmoteka.ru/{INN}", status=200,
        headers={"content-type": "text/html"}, retrieved_at=NOW,
    )
    second, _ = worker._store_raw(
        raw_root=tmp_path, content=_page(), kind="company",
        url=f"https://firmoteka.ru/{INN}", status=200,
        headers={"content-type": "text/html"}, retrieved_at=NOW,
    )
    assert first.checksum == second.checksum == manifest["response_sha256"]
    assert first.artifact_reference == second.artifact_reference


def test_same_body_keeps_each_retrieval_as_an_immutable_observation(tmp_path):
    first, _ = worker._store_raw(
        raw_root=tmp_path,
        content=_page(),
        kind="company",
        url=f"https://firmoteka.ru/{INN}",
        status=200,
        headers={"content-type": "text/html", "date": "first"},
        retrieved_at=NOW,
    )
    second, _ = worker._store_raw(
        raw_root=tmp_path,
        content=_page(),
        kind="company",
        url=f"https://firmoteka.ru/{INN}",
        status=200,
        headers={"content-type": "text/html", "date": "second"},
        retrieved_at=NOW + timedelta(days=1),
    )

    artifact_dir = tmp_path / worker.SOURCE_ID / first.checksum
    manifests = tuple(artifact_dir.glob("manifest-*.json"))
    assert first.artifact_reference == second.artifact_reference
    assert len(manifests) == 2
    assert manifests[0].read_bytes() != manifests[1].read_bytes()


def test_gzip_sitemap_is_decoded_for_discovery_but_raw_keeps_wire_bytes(
    monkeypatch, tmp_path
):
    sitemap = b"""<?xml version="1.0"?><sitemapindex>
    <sitemap><loc>https://firmoteka.ru/sitemap-catalogs.xml</loc></sitemap>
    </sitemapindex>"""
    compressed = gzip.compress(sitemap)
    responses = iter(
        (
            (
                b"User-agent: *\nAllow: /\nSitemap: https://firmoteka.ru/sitemap.xml\n",
                200,
                {"content-type": "text/plain"},
                NOW,
            ),
            (
                compressed,
                200,
                {"content-type": "text/xml; charset=utf-8"},
                NOW,
            ),
        )
    )
    monkeypatch.setattr(worker, "_fetch", lambda *_args, **_kwargs: next(responses))

    class Context:
        schedule_metadata = {
            "phase": "discovery",
            "crawl_run_id": str(uuid4()),
            "raw_root": str(tmp_path),
            "not_before": NOW.isoformat(),
        }

        def report_counters(self, _counters):
            return None

        def heartbeat(self):
            return None

    result = worker.firmoteka_worker_handler(Context())
    validation = result.staging_result.validation.metadata

    assert "https://firmoteka.ru/sitemap-catalogs.xml" in validation["documents"]
    assert result.raw_artifacts[-1].checksum == sha256(compressed).hexdigest()
    raw_path = result.raw_artifacts[-1].artifact_reference.removeprefix("file://")
    assert gzip.open(raw_path, "rb").read() == compressed


def test_provisional_master_never_overwrites_official_identity():
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            ensure_source_factory_datasets(session)
            dataset = session.scalar(select(DataSet).where(DataSet.code == "firmoteka"))
            source = session.scalar(select(DataSource).where(DataSource.code == "firmoteka"))
            official = Company(
                inn=INN, name="OFFICIAL NAME", entity_type="legal",
                master_authority="official", master_source="fns_egrul",
                official_registry_verified=True,
            )
            session.add(official)
            session.flush()
            row = {
                "inn": INN,
                "url": f"https://firmoteka.ru/{INN}",
                "raw_sha256": "a" * 64,
                "content_hash": "b" * 64,
                "retrieved_at": NOW.isoformat(),
                "projection": {
                    "name": "BRIDGE NAME", "full_name": "BRIDGE FULL",
                    "entity_type": "legal", "status_normalized": "ACTIVE",
                    "registration_date": "2002-08-13", "manager": "ИВАНОВ ИВАН",
                    "manager_position": "ДИРЕКТОР", "facts": [],
                },
            }
            created, _changed, _legal = worker._apply_master(
                session, dataset=dataset, source=source,
                claim=SimpleNamespace(run_id=None), row=row, now=NOW,
            )
            session.flush()
            assert created is False
            assert official.name == "OFFICIAL NAME"
            assert official.master_source == "fns_egrul"
            assert official.official_registry_verified is True
            assert official.master_provenance["firmoteka"]["raw_sha256"] == "a" * 64
    finally:
        transaction.rollback()
        connection.close()


def test_schedule_is_durable_idempotent_and_fixed_at_four_seconds(tmp_path):
    connection, transaction, factory = _factory()
    try:
        with factory() as session:
            ensure_source_factory_datasets(session)
            if session.get(WorkerHandlerRegistration, (worker.SOURCE_ID, worker.HANDLER_VERSION)) is None:
                session.add(
                    WorkerHandlerRegistration(
                    source_id=worker.SOURCE_ID,
                    handler_version=worker.HANDLER_VERSION,
                    approved=True, enabled=True, live_mode=False, metadata_json={},
                    )
                )
            session.flush()
            first = worker.schedule_firmoteka_check(session, raw_root=tmp_path, now=NOW)
            second = worker.schedule_firmoteka_check(session, raw_root=tmp_path, now=NOW)
            crawl = session.scalar(select(FirmotekaCrawlRun))
            assert first.created is True
            assert second.created is False
            assert first.job.id == second.job.id
            assert crawl.request_delay_seconds == 4
            assert crawl.concurrency == 1
            assert first.job.schedule_metadata["phase"] == "discovery"
    finally:
        transaction.rollback()
        connection.close()
