import importlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_tax_debt_pipeline as pipeline
from app.models.company import Company
from app.models.source import DataSet, DataSource
from app.models.tax_debt import (
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
    FnsTaxDebtRawArtifact,
)
from app.models.worker import WorkerJob, WorkerPublicationState, WorkerRawManifest
from app.providers.fns_tax_debt_provider import (
    TaxDebtDiscovery,
    TaxDebtOfficialRelease,
)
from app.services import tax_debt_service
from app.services.tax_debt_service import prepare_tax_debt_public_projection
from app.sources.fns_tax_debt import (
    CONTROLLED_LIVE_HANDLER_VERSION,
    CONTROLLED_LIVE_PILOT_ENABLED,
    FACT_CODE,
    FNS_TAX_DEBT_SOURCE_CONTRACT,
    MASS_INGESTION_ENABLED,
    OFFICIAL_SOURCE_PAGE,
    PILOT_ENVIRONMENT,
    SOURCE_ID,
)
from app.worker.contracts import HandlerContext
from app.worker.errors import (
    HandlerNotRegisteredError,
    LegalBlockError,
    TemporaryInfrastructureError,
    WorkerTimeoutError,
)
from app.worker.execution import (
    RetryPolicy,
    WorkerExecutor,
    create_job,
    register_handler,
)
from app.worker.registry import HandlerRegistry

pilot_migration = importlib.import_module(
    "migrations.versions.e1f2a3b4c5d6_add_s02_controlled_live_pilot"
)


SOURCE_AS_OF = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 9, 23, 9, tzinfo=timezone.utc)
VALID_INN = "7707083893"


def _document(
    *,
    inn: str = VALID_INN,
    document_id: str = "DEBT-1",
    data_date: str = "01.08.2026",
    total: str = "125.00",
    arrears: str = "100.00",
    penalties: str = "20.00",
    fines: str = "5.00",
) -> str:
    return f"""
    <Документ ИдДок="{document_id}" ДатаДок="25.08.2026" ДатаСост="{data_date}">
      <СведНП ИННЮЛ="{inn}" НаимОрг="ООО ТЕСТ" />
      <СведНедоим НаимНалог="Налог на прибыль"
        СумНедНалог="{arrears}" СумПени="{penalties}"
        СумШтраф="{fines}" ОбщСумНедоим="{total}" />
    </Документ>
    """


def _zip(path: Path, documents: list[str], *, root: str = "Файл", version=True) -> Path:
    version_attribute = ' ВерсФорм="4.01"' if version else ""
    xml = (
        f'<{root}{version_attribute} ИдФайл="fixture" '
        f'ТипИнф="ОТКРДАННЫЕ6" КолДок="{len(documents)}">'
        f'{"".join(documents)}</{root}>'
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("data-1.xml", xml.encode("utf-8"))
    return path


def _context(tmp_path: Path, source_path: Path, *, deadline=None) -> HandlerContext:
    return HandlerContext(
        job_id=uuid4(),
        run_id=uuid4(),
        source_id=SOURCE_ID,
        worker_id="dev009-test",
        fencing_token=1,
        deadline_at=deadline or datetime.now(timezone.utc) + timedelta(minutes=1),
        schedule_metadata={
            "source_path": str(source_path),
            "artifact_store": str(tmp_path / "raw"),
            "source_as_of": SOURCE_AS_OF.isoformat(),
            "retrieved_at": RETRIEVED_AT.isoformat(),
            "mode": "fixture",
        },
        heartbeat=lambda: None,
        report_counters=lambda _counters: None,
        shutdown_requested=lambda: False,
    )


def test_source_contract_is_official_bounded_and_provenance_complete():
    contract = FNS_TAX_DEBT_SOURCE_CONTRACT

    assert contract.source_id == "S02"
    assert contract.fact_code == "tax.debt.amount_as_of_date"
    assert contract.official_source.startswith("https://www.nalog.gov.ru/opendata/")
    assert contract.identifiers == ("10-digit legal-entity INN",)
    assert "source_as_of" in contract.provenance_fields
    assert "retrieved_at" in contract.provenance_fields
    assert CONTROLLED_LIVE_PILOT_ENABLED is False
    assert MASS_INGESTION_ENABLED is False


def test_fixture_handler_builds_replayable_raw_manifest_and_staging(tmp_path):
    source = _zip(tmp_path / "fixture.zip", [_document()])

    result = pipeline.fns_tax_debt_handler(_context(tmp_path, source))

    assert result.counters.records_seen == 1
    assert result.counters.records_written == 1
    assert result.counters.records_published == 0
    assert len(result.raw_artifacts) == 1
    raw = result.raw_artifacts[0]
    assert raw.manifest["source_id"] == "S02"
    assert raw.manifest["source_as_of"] == SOURCE_AS_OF.isoformat()
    assert raw.manifest["retrieved_at"] == RETRIEVED_AT.isoformat()
    assert pipeline.calculate_sha256(pipeline._file_uri_to_path(raw.artifact_reference))[0] == raw.checksum
    replay = pipeline.load_staged_normalization(
        result.staging_result.staging_pointer,
        expected_sha256=raw.checksum,
    )
    assert replay["entries"][0]["total_debt"] == "125.00"


def test_raw_checksum_rejects_tampered_expectation(tmp_path):
    source = _zip(tmp_path / "fixture.zip", [_document()])

    with pytest.raises(pipeline.TaxDebtParseError, match="checksum mismatch"):
        pipeline.stage_tax_debt_artifact(
            source,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
            expected_sha256="0" * 64,
        )


def test_parser_failure_is_fail_closed_for_malformed_xml(tmp_path):
    source = tmp_path / "malformed.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr("bad.xml", b'<File><broken></File>')

    with pytest.raises(pipeline.TaxDebtParseError, match="cannot parse XML"):
        pipeline.parse_tax_debt_zip(source)


@pytest.mark.parametrize(
    ("field", "attribute", "reason_code"),
    [
        ("ИдДок", ' ИдДок="DEBT-1"', "missing_document_id"),
        ("ДатаДок", ' ДатаДок="25.08.2026"', "missing_document_date"),
        ("ОбщСумНедоим", ' ОбщСумНедоим="125.00"', "missing_total_debt"),
    ],
)
def test_missing_required_xml_field_is_quarantined_without_normalization(
    tmp_path,
    field,
    attribute,
    reason_code,
):
    document = _document().replace(attribute, "")
    source = _zip(tmp_path / f"missing-{field}.zip", [document])

    parsed = pipeline.parse_tax_debt_zip(source)

    assert parsed.entries == ()
    assert parsed.coverage["normalized_records"] == 0
    assert parsed.coverage["quarantined_records"] == 1
    assert reason_code in parsed.quarantine[0]["reason_codes"]


@pytest.mark.parametrize(
    "root,version",
    [("Unexpected", True), ("Файл", False)],
)
def test_schema_mismatch_is_not_silently_quarantined(tmp_path, root, version):
    source = _zip(
        tmp_path / f"schema-{root}-{version}.zip",
        [_document()],
        root=root,
        version=version,
    )

    with pytest.raises(pipeline.TaxDebtSchemaError):
        pipeline.parse_tax_debt_zip(source)


def test_invalid_inn_is_quarantined_and_never_normalized(tmp_path):
    source = _zip(tmp_path / "invalid-inn.zip", [_document(inn="7707083894")])

    parsed = pipeline.parse_tax_debt_zip(source)

    assert parsed.entries == ()
    assert parsed.coverage["quarantined_records"] == 1
    assert parsed.quarantine[0]["reason_codes"] == ("invalid_inn",)


def test_identical_duplicate_is_deterministically_deduplicated(tmp_path):
    document = _document()
    source = _zip(tmp_path / "duplicates.zip", [document, document])

    first = pipeline.parse_tax_debt_zip(source)
    second = pipeline.parse_tax_debt_zip(source)

    assert second == first
    assert len(first.entries) == 1
    assert first.coverage["identical_duplicates"] == 1


def test_conflicting_duplicate_identity_is_quarantined(tmp_path):
    source = _zip(
        tmp_path / "identity-conflict.zip",
        [
            _document(document_id="DEBT-1"),
            _document(
                document_id="DEBT-2",
                arrears="200.00",
                penalties="20.00",
                fines="5.00",
                total="225.00",
            ),
        ],
    )

    parsed = pipeline.parse_tax_debt_zip(source)

    assert parsed.entries == ()
    assert parsed.coverage["identity_conflicts"] == 2
    assert all(
        row["reason_codes"] == ("duplicate_identity_conflict",)
        for row in parsed.quarantine
    )


def test_entity_matching_has_matched_unmatched_and_conflict_states():
    matched = pipeline.resolve_inn_match(
        VALID_INN, {VALID_INN: ((10, "legal"),)}
    )
    unmatched = pipeline.resolve_inn_match(VALID_INN, {})
    duplicate_conflict = pipeline.resolve_inn_match(
        VALID_INN, {VALID_INN: ((10, "legal"), (11, "legal"))}
    )
    type_conflict = pipeline.resolve_inn_match(
        VALID_INN, {VALID_INN: ((12, "individual_entrepreneur"),)}
    )

    assert matched == pipeline.InnMatch(10, "matched", "inn_exact")
    assert unmatched == pipeline.InnMatch(None, "unmatched", None)
    assert duplicate_conflict == pipeline.InnMatch(None, "conflict", None)
    assert type_conflict == pipeline.InnMatch(None, "conflict", None)


def test_transient_failure_is_retryable_under_worker_policy():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=5)

    assert policy.allows(
        TemporaryInfrastructureError("artifact store unavailable"),
        attempt_no=1,
        max_attempts=3,
    ) is True
    assert policy.delay(1) == timedelta(seconds=1)


def test_handler_obeys_worker_deadline_before_touching_source(tmp_path):
    source = _zip(tmp_path / "fixture.zip", [_document()])
    context = _context(
        tmp_path,
        source,
        deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    with pytest.raises(WorkerTimeoutError):
        pipeline.fns_tax_debt_handler(context)


def test_raw_and_normalized_replay_are_idempotent(tmp_path):
    source = _zip(tmp_path / "fixture.zip", [_document()])
    first = pipeline.fns_tax_debt_handler(_context(tmp_path, source))
    raw_path = pipeline._file_uri_to_path(first.raw_artifacts[0].artifact_reference)
    staging_path = pipeline._file_uri_to_path(first.staging_result.staging_pointer)
    mtimes = (raw_path.stat().st_mtime_ns, staging_path.stat().st_mtime_ns)

    second = pipeline.fns_tax_debt_handler(_context(tmp_path, source))

    assert second == first
    assert (raw_path.stat().st_mtime_ns, staging_path.stat().st_mtime_ns) == mtimes


def test_controlled_live_pilot_is_disabled_by_default(tmp_path):
    source = _zip(tmp_path / "fixture.zip", [_document()])

    with pytest.raises(LegalBlockError, match="disabled"):
        pipeline.stage_tax_debt_artifact(
            source,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
            mode="controlled_live",
        )


def test_fact_provenance_and_public_projection_keep_dates_and_limitations():
    record = {
        "source_document_id": "DEBT-1",
        "source_member": "data-1.xml",
        "record_hash": "a" * 64,
    }
    artifact = SimpleNamespace(
        sha256="b" * 64,
        artifact_reference="file:///raw/artifact.zip",
        source_as_of=SOURCE_AS_OF,
        retrieved_at=RETRIEVED_AT,
    )

    provenance = pipeline.build_fact_provenance(
        record,
        artifact=artifact,
        run_id=uuid4(),
        match_method="inn_exact",
    )
    projection = prepare_tax_debt_public_projection(
        {
            "fact_code": FACT_CODE,
            "result": "found",
            "applicable": True,
            "has_debt": True,
            "total_debt": Decimal("125.00"),
            "data_date": SOURCE_AS_OF.date(),
            "source": "fns_tax_debt",
            "source_reference": "file:///raw/artifact.zip#record=" + "a" * 64,
            "provenance": provenance,
            "limitation_states": list(pipeline.LIMITATION_STATES),
            "retrieved_at": RETRIEVED_AT,
        }
    )

    assert provenance["source_id"] == "S02"
    assert provenance["artifact_sha256"] == "b" * 64
    assert provenance["matching_method"] == "inn_exact"
    assert projection["fact_code"] == "tax.debt.amount_as_of_date"
    assert projection["amount"] == Decimal("125.00")
    assert projection["amount_as_of_date"] == SOURCE_AS_OF.date()
    assert "not_real_time_balance" in projection["limitation_states"]


@pytest.fixture
def pipeline_db():
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


def _valid_inn(seed: int) -> str:
    base = f"{seed % 1_000_000_000:09d}"
    digits = [int(char) for char in base]
    check = sum(
        value * weight
        for value, weight in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))
    ) % 11 % 10
    return base + str(check)


def _ensure_dataset_and_company(factory, *, inn: str) -> tuple[int, int]:
    with factory() as session:
        dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_tax_debt"))
        if dataset is None:
            source = session.scalar(select(DataSource).where(DataSource.code == "fns"))
            if source is None:
                source = DataSource(
                    code="fns",
                    name="ФНС России",
                    source_type="official",
                    priority=10,
                    enabled=True,
                )
                session.add(source)
                session.flush()
            dataset = DataSet(
                source_id=source.id,
                code="fns_tax_debt",
                name="ФНС: Налоговая задолженность",
                domain="tax_debt",
                update_mode="bulk",
                data_format="xml",
                refresh_schedule="periodic",
                priority=10,
                enabled=False,
            )
            session.add(dataset)
            session.flush()
        company = Company(
            inn=inn,
            name=f"DEV-009 {inn}",
            entity_type="legal",
            source="dev009_fixture",
        )
        session.add(company)
        session.commit()
        return dataset.id, company.id


def _publish_then_transient_failure(session, claim, result):
    pipeline.publish_tax_debt_result(session, claim, result)
    raise TemporaryInfrastructureError("simulated publication outage")


def test_full_worker_pipeline_publishes_canonical_fact_and_is_idempotent(
    pipeline_db,
    tmp_path,
):
    inn = _valid_inn(uuid4().int)
    dataset_id, company_id = _ensure_dataset_and_company(pipeline_db, inn=inn)
    source = _zip(tmp_path / "vertical.zip", [_document(inn=inn)])
    registry = HandlerRegistry()
    with pipeline_db() as session:
        pipeline.register_fns_tax_debt_handler(session, registry)
        first = pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=source,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
        )
        second = pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=source,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
        )
        assert first.created is True
        assert second.created is False
        assert second.job.id == first.job.id
        session.commit()

    executor = WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev009-integration",
        process_start_method="fork",
    )
    run_id = executor.run_once()

    with pipeline_db() as session:
        job = session.get(WorkerJob, first.job.id)
        snapshot = session.scalar(
            select(CompanyTaxDebtSnapshot).where(
                CompanyTaxDebtSnapshot.company_id == company_id,
                CompanyTaxDebtSnapshot.dataset_id == dataset_id,
            )
        )
        normalized = session.scalar(
            select(FnsTaxDebtNormalizedRecord).where(
                FnsTaxDebtNormalizedRecord.company_id == company_id
            )
        )
        raw = session.scalar(
            select(FnsTaxDebtRawArtifact).where(
                FnsTaxDebtRawArtifact.dataset_id == dataset_id
            )
        )
        worker_manifest = session.scalar(
            select(WorkerRawManifest).where(WorkerRawManifest.run_id == run_id)
        )
        publication = session.get(WorkerPublicationState, SOURCE_ID)

        assert job.status == "succeeded"
        assert raw.sha256 == worker_manifest.checksum
        assert normalized.match_state == "matched"
        assert normalized.match_method == "inn_exact"
        assert snapshot.fact_code == FACT_CODE
        assert snapshot.total_debt == Decimal("125.00")
        assert snapshot.normalized_record_id == normalized.id
        assert snapshot.provenance["worker_run_id"] == str(run_id)
        assert snapshot.provenance["artifact_sha256"] == raw.sha256
        assert snapshot.source_reference.startswith(raw.artifact_reference)
        assert "not_real_time_balance" in snapshot.limitation_states
        assert publication.active_pointer.endswith("normalized.json")
        assert publication.validation_metadata["validation"]["fact_code"] == FACT_CODE


def test_new_full_snapshot_without_previous_inn_advances_active_dataset(
    pipeline_db,
    tmp_path,
    monkeypatch,
):
    previous_inn = _valid_inn(uuid4().int)
    absent_inn = _valid_inn(uuid4().int)
    while absent_inn == previous_inn:
        absent_inn = _valid_inn(uuid4().int)
    dataset_id, company_id = _ensure_dataset_and_company(
        pipeline_db,
        inn=previous_inn,
    )
    old_source = _zip(
        tmp_path / "old-found.zip",
        [_document(inn=previous_inn, data_date="01.08.2026")],
    )
    new_source = _zip(
        tmp_path / "new-without-inn.zip",
        [
            _document(
                inn=absent_inn,
                document_id="DEBT-NEW-OTHER",
                data_date="01.09.2026",
            )
        ],
    )
    new_source_as_of = SOURCE_AS_OF + timedelta(days=31)
    new_retrieved_at = RETRIEVED_AT + timedelta(days=31)
    registry = HandlerRegistry()

    with pipeline_db() as session:
        pipeline.register_fns_tax_debt_handler(session, registry)
        pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=old_source,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
        )
        session.commit()
    executor = WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev009-freshness-old",
        process_start_method="fork",
    )
    executor.run_once()

    with pipeline_db() as session:
        pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=new_source,
            artifact_store=tmp_path / "raw",
            source_as_of=new_source_as_of,
            retrieved_at=new_retrieved_at,
        )
        session.commit()
    executor = WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev009-freshness-new",
        process_start_method="fork",
    )
    executor.run_once()

    with pipeline_db() as session:
        dataset = session.get(DataSet, dataset_id)
        assert dataset.last_data_date.isoformat() == "2026-09-01"
        assert dataset.source_as_of == new_source_as_of
        assert dataset.retrieved_at == new_retrieved_at
        assert dataset.coverage["matched_records"] == 0
        assert dataset.coverage["unmatched_records"] == 1
        assert session.scalar(
            select(func.count()).select_from(CompanyTaxDebtSnapshot).where(
                CompanyTaxDebtSnapshot.company_id == company_id,
                CompanyTaxDebtSnapshot.dataset_id == dataset_id,
            )
        ) == 1

    monkeypatch.setattr(tax_debt_service, "get_session", pipeline_db)
    check = tax_debt_service.get_tax_debt_check_for_company(company_id)

    assert check["state"] == "STALE_DATA"
    assert check["result"] == "unavailable"
    assert check["reason"] == "freshness_metadata_missing"
    assert check["data_date"].isoformat() == "2026-09-01"
    assert check["total_debt"] == Decimal("0.00")


def test_transient_publication_failure_rolls_back_domain_rows_and_schedules_retry(
    pipeline_db,
    tmp_path,
):
    inn = _valid_inn(uuid4().int)
    dataset_id, company_id = _ensure_dataset_and_company(pipeline_db, inn=inn)
    previous_source_as_of = SOURCE_AS_OF - timedelta(days=31)
    previous_retrieved_at = RETRIEVED_AT - timedelta(days=31)
    previous_data_date = datetime(2026, 7, 1).date()
    with pipeline_db() as session:
        dataset = session.get(DataSet, dataset_id)
        dataset.last_data_date = previous_data_date
        dataset.source_as_of = previous_source_as_of
        dataset.retrieved_at = previous_retrieved_at
        previous_snapshot = CompanyTaxDebtSnapshot(
            company_id=company_id,
            dataset_id=dataset_id,
            data_date=previous_data_date,
            source_document_id="PREVIOUS-ACTIVE",
            total_arrears=Decimal("40.00"),
            total_penalties=Decimal("5.00"),
            total_fines=Decimal("0.00"),
            total_debt=Decimal("45.00"),
            item_count=0,
            source_reference="fixture://previous-active",
            provenance={"artifact_sha256": "a" * 64},
            limitation_states=list(pipeline.LIMITATION_STATES),
            retrieved_at=previous_retrieved_at,
        )
        session.add(previous_snapshot)
        session.commit()
        previous_snapshot_id = previous_snapshot.id
    source = _zip(
        tmp_path / "rollback.zip",
        [_document(inn=inn, document_id="ROLLBACK-1", data_date="02.08.2026")],
    )
    checksum, _ = pipeline.calculate_sha256(source)
    registry = HandlerRegistry()
    version = "fns-tax-debt-v1-rollback-test"
    metadata = {
        "source_path": str(source.resolve()),
        "artifact_store": str((tmp_path / "raw").resolve()),
        "source_as_of": SOURCE_AS_OF.isoformat(),
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "expected_sha256": checksum,
        "mode": "fixture",
    }
    with pipeline_db() as session:
        register_handler(
            session,
            registry,
            source_id=SOURCE_ID,
            version=version,
            handler=pipeline.fns_tax_debt_handler,
            publisher=_publish_then_transient_failure,
            approved=True,
            fixture=True,
            metadata={"task": "DEV-009-rollback-test"},
        )
        creation = create_job(
            session,
            source_id=SOURCE_ID,
            job_type="fns_tax_debt_fixture",
            handler_version=version,
            idempotency_key=f"rollback:{uuid4().hex}",
            schedule_metadata=metadata,
            max_attempts=2,
            timeout_seconds=30,
        )
        session.commit()

    executor = WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev009-rollback",
        retry_policy=RetryPolicy(base_delay_seconds=1, max_delay_seconds=2),
        process_start_method="fork",
    )
    with pytest.raises(TemporaryInfrastructureError, match="publication outage"):
        executor.run_once()

    with pipeline_db() as session:
        job = session.get(WorkerJob, creation.job.id)
        dataset = session.get(DataSet, dataset_id)
        assert job.status == "retry_scheduled"
        assert dataset.last_data_date == previous_data_date
        assert dataset.source_as_of == previous_source_as_of
        assert dataset.retrieved_at == previous_retrieved_at
        assert session.scalar(
            select(func.count()).select_from(FnsTaxDebtRawArtifact).where(
                FnsTaxDebtRawArtifact.dataset_id == dataset_id,
                FnsTaxDebtRawArtifact.sha256 == checksum,
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(FnsTaxDebtNormalizedRecord).where(
                FnsTaxDebtNormalizedRecord.company_id == company_id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(CompanyTaxDebtSnapshot).where(
                CompanyTaxDebtSnapshot.company_id == company_id,
                CompanyTaxDebtSnapshot.dataset_id == dataset_id,
            )
        ) == 1
        preserved = session.get(CompanyTaxDebtSnapshot, previous_snapshot_id)
        assert preserved.source_document_id == "PREVIOUS-ACTIVE"
        assert preserved.total_debt == Decimal("45.00")


def _controlled_xsd(path: Path) -> Path:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="Файл">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="Документ" minOccurs="1" maxOccurs="unbounded">
                  <xs:complexType>
                    <xs:sequence>
                      <xs:element name="СведНП">
                        <xs:complexType>
                          <xs:attribute name="ИННЮЛ" type="xs:string" use="required"/>
                          <xs:attribute name="НаимОрг" type="xs:string"/>
                        </xs:complexType>
                      </xs:element>
                      <xs:element name="СведНедоим" minOccurs="1" maxOccurs="unbounded">
                        <xs:complexType>
                          <xs:attribute name="НаимНалог" type="xs:string"/>
                          <xs:attribute name="СумНедНалог" type="xs:decimal"/>
                          <xs:attribute name="СумПени" type="xs:decimal"/>
                          <xs:attribute name="СумШтраф" type="xs:decimal"/>
                          <xs:attribute name="ОбщСумНедоим" type="xs:decimal" use="required"/>
                        </xs:complexType>
                      </xs:element>
                    </xs:sequence>
                    <xs:attribute name="ИдДок" type="xs:string" use="required"/>
                    <xs:attribute name="ДатаДок" type="xs:string" use="required"/>
                    <xs:attribute name="ДатаСост" type="xs:string" use="required"/>
                  </xs:complexType>
                </xs:element>
              </xs:sequence>
              <xs:attribute name="ВерсФорм" type="xs:string" use="required"/>
              <xs:attribute name="ИдФайл" type="xs:string" use="required"/>
              <xs:attribute name="ТипИнф" type="xs:string" use="required"/>
              <xs:attribute name="КолДок" type="xs:integer" use="required"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """,
        encoding="utf-8",
    )
    return path


def _controlled_discovery(
    *,
    artifact_date: str,
    data_as_of: date,
    actual_until: date,
    discovered_at: datetime,
) -> TaxDebtDiscovery:
    source_as_of = datetime.strptime(artifact_date, "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    return TaxDebtDiscovery(
        release=TaxDebtOfficialRelease(
            discovery_page_url=OFFICIAL_SOURCE_PAGE,
            artifact_url=(
                "https://file.nalog.ru/opendata/7707329152-debtam/"
                f"data-{artifact_date}-structure-20181201.zip"
            ),
            xsd_url=(
                "https://file.nalog.ru/opendata/7707329152-debtam/"
                "structure-20181201.xsd"
            ),
            source_as_of=source_as_of,
            data_as_of=data_as_of,
            official_actual_until=actual_until,
            metadata={},
        ),
        changed=True,
        discovered_at=discovered_at,
    )


def test_controlled_live_same_artifact_creates_one_job(
    pipeline_db,
    tmp_path,
):
    inn = _valid_inn(uuid4().int)
    _ensure_dataset_and_company(pipeline_db, inn=inn)
    source = _zip(tmp_path / "controlled-idempotent.zip", [_document(inn=inn)])
    xsd = _controlled_xsd(tmp_path / "structure.xsd")
    discovery = _controlled_discovery(
        artifact_date="20260825",
        data_as_of=date(2026, 8, 1),
        actual_until=date(2026, 9, 25),
        discovered_at=RETRIEVED_AT,
    )
    config = pipeline.ControlledLivePilotConfig(
        enabled=True,
        environment=PILOT_ENVIRONMENT,
        cohort_inns=frozenset({inn}),
        handler_version=CONTROLLED_LIVE_HANDLER_VERSION,
    )
    with pipeline_db() as session:
        pipeline.approve_fns_tax_debt_controlled_live_handler(
            session,
            approved_by="DEV-010 test",
            approved_at=RETRIEVED_AT,
        )
        first = pipeline.enqueue_fns_tax_debt_controlled_live_job(
            session,
            source_path=source,
            xsd_path=xsd,
            artifact_store=tmp_path / "raw",
            discovery=discovery,
            config=config,
            retrieved_at=RETRIEVED_AT,
        )
        second = pipeline.enqueue_fns_tax_debt_controlled_live_job(
            session,
            source_path=source,
            xsd_path=xsd,
            artifact_store=tmp_path / "raw",
            discovery=discovery,
            config=config,
            retrieved_at=RETRIEVED_AT,
        )

        assert first.created is True
        assert second.created is False
        assert second.job.id == first.job.id


def test_controlled_live_handler_requires_explicit_durable_approval(pipeline_db):
    registry = HandlerRegistry()

    with pipeline_db() as session:
        with pytest.raises(HandlerNotRegisteredError, match="explicit durable"):
            pipeline.register_fns_tax_debt_controlled_live_handler(session, registry)


def test_controlled_live_first_transition_rollback_restores_baseline_a(
    pipeline_db,
    tmp_path,
    monkeypatch,
):
    inn = _valid_inn(uuid4().int)
    dataset_id, company_id = _ensure_dataset_and_company(pipeline_db, inn=inn)
    xsd = _controlled_xsd(tmp_path / "structure.xsd")
    source_a = _zip(
        tmp_path / "generation-a.zip",
        [_document(inn=inn, document_id="GEN-A", data_date="01.08.2026")],
    )
    source_b = _zip(
        tmp_path / "generation-b.zip",
        [
            _document(
                inn=inn,
                document_id="GEN-B",
                data_date="01.09.2026",
                arrears="200.00",
                penalties="20.00",
                fines="5.00",
                total="225.00",
            )
        ],
    )
    retrieved_b = RETRIEVED_AT + timedelta(days=1)
    discovery_b = _controlled_discovery(
        artifact_date="20260920",
        data_as_of=date(2026, 9, 1),
        actual_until=date(2026, 10, 20),
        discovered_at=retrieved_b,
    )
    config = pipeline.ControlledLivePilotConfig(
        enabled=True,
        environment=PILOT_ENVIRONMENT,
        cohort_inns=frozenset({inn}),
        handler_version=CONTROLLED_LIVE_HANDLER_VERSION,
    )
    registry = HandlerRegistry()
    with pipeline_db() as session:
        pipeline.register_fns_tax_debt_handler(session, registry)
        pipeline.approve_fns_tax_debt_controlled_live_handler(
            session,
            approved_by="DEV-010 rollback test",
            approved_at=RETRIEVED_AT,
        )
        pipeline.register_fns_tax_debt_controlled_live_handler(session, registry)
        pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=source_a,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
        )
        session.commit()

    executor = WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev010-baseline-a",
        process_start_method="fork",
    )
    executor.run_once()
    checksum_a, _ = pipeline.calculate_sha256(source_a)

    with pipeline_db() as session:
        pipeline.enqueue_fns_tax_debt_controlled_live_job(
            session,
            source_path=source_b,
            xsd_path=xsd,
            artifact_store=tmp_path / "raw",
            discovery=discovery_b,
            config=config,
            retrieved_at=retrieved_b,
        )
        session.commit()
    executor = WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev010-generation-b",
        process_start_method="fork",
    )
    executor.run_once()

    monkeypatch.setattr(tax_debt_service, "get_session", pipeline_db)
    monkeypatch.setattr(
        tax_debt_service,
        "utc_now",
        lambda: datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    before = tax_debt_service.get_tax_debt_check_for_company(company_id)
    assert before["data_date"] == date(2026, 9, 1)
    assert before["state"] == "FOUND"
    assert before["total_debt"] == Decimal("225.00")

    with pipeline_db() as session:
        state = session.get(FnsTaxDebtPilotState, SOURCE_ID)
        dataset = session.get(DataSet, dataset_id)
        assert state.generation == 2
        assert state.baseline_data_date == date(2026, 8, 1)
        assert state.active_data_date == date(2026, 9, 1)
        assert state.normalized_generation == 1
        assert state.fact_generation == 1
        assert state.query_generation == 1
        assert dataset.source_as_of == SOURCE_AS_OF
        assert dataset.retrieved_at == RETRIEVED_AT
        assert dataset.last_data_date == date(2026, 8, 1)
        pipeline.rollback_fns_tax_debt_generation(
            session,
            expected_generation=state.generation,
            now=retrieved_b + timedelta(hours=1),
        )
        session.commit()

    after = tax_debt_service.get_tax_debt_check_for_company(company_id)
    assert after["data_date"] == date(2026, 8, 1)
    assert after["state"] == "STALE_DATA"
    assert after["reason"] == "freshness_metadata_missing"
    assert after["total_debt"] == Decimal("0.00")

    with pipeline_db() as session:
        dataset = session.get(DataSet, dataset_id)
        state = session.get(FnsTaxDebtPilotState, SOURCE_ID)
        pointer = session.get(WorkerPublicationState, SOURCE_ID)
        generations = session.scalars(
            select(FnsTaxDebtPublicationGeneration)
            .where(FnsTaxDebtPublicationGeneration.dataset_id == dataset_id)
            .order_by(FnsTaxDebtPublicationGeneration.generation)
        ).all()
        monitoring = pipeline.get_fns_tax_debt_pilot_monitoring(
            session,
            now=datetime(2026, 9, 26, tzinfo=timezone.utc),
        )

        assert dataset.source_as_of == SOURCE_AS_OF
        assert dataset.retrieved_at == RETRIEVED_AT
        assert dataset.last_data_date == date(2026, 8, 1)
        assert state.active_checksum == checksum_a
        assert state.generation == 3
        assert state.normalized_generation == 0
        assert state.fact_generation == 0
        assert state.query_generation == 0
        assert state.active_source_as_of == SOURCE_AS_OF
        assert state.active_retrieved_at == RETRIEVED_AT
        assert pointer.active_pointer == generations[0].staging_pointer
        assert pointer.rollback_pointer == generations[1].staging_pointer
        assert state.active_raw_pointer == generations[0].raw_pointer
        assert [row.status for row in generations] == ["active", "rollback"]
        assert [row.publication_scope for row in generations] == ["baseline", "pilot"]
        assert monitoring["last_discovery"] == discovery_b.discovered_at
        assert monitoring["last_success"] is not None
        assert monitoring["active_checksum"] == checksum_a
        assert monitoring["generation"] == 3
        assert monitoring["normalized_generation"] == 0
        assert monitoring["fact_generation"] == 0
        assert monitoring["query_generation"] == 0
        assert monitoring["data_date"] == date(2026, 8, 1)
        assert monitoring["source_as_of"] == SOURCE_AS_OF
        assert monitoring["retrieved_at"] == RETRIEVED_AT
        assert monitoring["counters"]["records_published"] == 1
        assert monitoring["freshness"] == "unknown"
        assert monitoring["errors"] == []


def test_controlled_live_is_visible_inside_cohort_and_isolated_outside(
    pipeline_db,
    tmp_path,
    monkeypatch,
):
    cohort_inn = _valid_inn(uuid4().int)
    outside_inn = _valid_inn(uuid4().int)
    while outside_inn == cohort_inn:
        outside_inn = _valid_inn(uuid4().int)
    dataset_id, cohort_company_id = _ensure_dataset_and_company(
        pipeline_db, inn=cohort_inn
    )
    _, outside_company_id = _ensure_dataset_and_company(pipeline_db, inn=outside_inn)
    baseline = _zip(
        tmp_path / "isolation-baseline.zip",
        [
            _document(inn=cohort_inn, document_id="COHORT-A"),
            _document(
                inn=outside_inn,
                document_id="OUTSIDE-A",
                arrears="40.00",
                penalties="0.00",
                fines="0.00",
                total="40.00",
            ),
        ],
    )
    pilot = _zip(
        tmp_path / "isolation-pilot.zip",
        [
            _document(
                inn=cohort_inn,
                document_id="COHORT-B",
                data_date="01.09.2026",
                arrears="200.00",
                penalties="20.00",
                fines="5.00",
                total="225.00",
            )
        ],
    )
    xsd = _controlled_xsd(tmp_path / "structure.xsd")
    discovery = _controlled_discovery(
        artifact_date="20260920",
        data_as_of=date(2026, 9, 1),
        actual_until=date(2026, 10, 20),
        discovered_at=RETRIEVED_AT,
    )
    config = pipeline.ControlledLivePilotConfig(
        enabled=True,
        cohort_inns=frozenset({cohort_inn}),
    )
    registry = HandlerRegistry()
    with pipeline_db() as session:
        before = session.scalar(select(func.count()).select_from(Company))
        pipeline.register_fns_tax_debt_handler(session, registry)
        pipeline.approve_fns_tax_debt_controlled_live_handler(
            session,
            approved_by="DEV-010 isolation test",
            approved_at=RETRIEVED_AT,
        )
        pipeline.register_fns_tax_debt_controlled_live_handler(session, registry)
        pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=baseline,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
        )
        session.commit()
    WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev010-isolation-baseline",
        process_start_method="fork",
    ).run_once()

    with pipeline_db() as session:
        pipeline.enqueue_fns_tax_debt_controlled_live_job(
            session,
            source_path=pilot,
            xsd_path=xsd,
            artifact_store=tmp_path / "raw",
            discovery=discovery,
            config=config,
            retrieved_at=RETRIEVED_AT,
        )
        session.commit()
    WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev010-isolation",
        process_start_method="fork",
    ).run_once()

    monkeypatch.setattr(tax_debt_service, "get_session", pipeline_db)
    monkeypatch.setattr(
        tax_debt_service,
        "utc_now",
        lambda: datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    inside = tax_debt_service.get_tax_debt_check_for_company(cohort_company_id)
    outside = tax_debt_service.get_tax_debt_check_for_company(outside_company_id)
    inside_history = tax_debt_service.get_tax_debt_history(cohort_company_id)
    outside_history = tax_debt_service.get_tax_debt_history(outside_company_id)
    assert inside["data_date"] == date(2026, 9, 1)
    assert inside["state"] == "FOUND"
    assert inside["total_debt"] == Decimal("225.00")
    assert outside["data_date"] == date(2026, 8, 1)
    assert outside["state"] == "STALE_DATA"
    assert outside["reason"] == "freshness_metadata_missing"
    assert outside["total_debt"] == Decimal("0.00")
    assert [row["total_debt"] for row in inside_history] == [Decimal("225.00")]
    assert [row["total_debt"] for row in outside_history] == [Decimal("40.00")]

    with pipeline_db() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == before
        state = session.get(FnsTaxDebtPilotState, SOURCE_ID)
        dataset = session.get(DataSet, dataset_id)
        outside_generations = session.scalars(
            select(CompanyTaxDebtSnapshot.publication_generation).where(
                CompanyTaxDebtSnapshot.company_id == outside_company_id
            )
        ).all()
        assert state.cohort_inns == [cohort_inn]
        assert state.counters["records_published"] == 1
        assert dataset.last_data_date == date(2026, 8, 1)
        assert outside_generations == [0]


def test_pilot_migration_empty_upgrade_downgrade_upgrade_cycle(pipeline_db):
    connection = pipeline_db.kw["bind"]
    with Operations.context(MigrationContext.configure(connection)):
        pilot_migration.downgrade()
        inspector = sa.inspect(connection)
        assert not inspector.has_table("fns_tax_debt_pilot_state")
        assert not inspector.has_table("fns_tax_debt_publication_generations")
        assert "publication_generation" not in {
            column["name"]
            for column in inspector.get_columns("company_tax_debt_snapshots")
        }

        pilot_migration.upgrade()
        inspector = sa.inspect(connection)
        assert inspector.has_table("fns_tax_debt_pilot_state")
        assert inspector.has_table("fns_tax_debt_publication_generations")
        assert "publication_generation" in {
            column["name"]
            for column in inspector.get_columns("company_tax_debt_snapshots")
        }


def test_pilot_migration_downgrade_restores_baseline_pointer_and_same_date_data(
    pipeline_db,
    tmp_path,
):
    inn = _valid_inn(uuid4().int)
    dataset_id, company_id = _ensure_dataset_and_company(pipeline_db, inn=inn)
    baseline = _zip(
        tmp_path / "migration-baseline.zip",
        [_document(inn=inn, document_id="MIGRATION-A")],
    )
    pilot_b = _zip(
        tmp_path / "migration-pilot-b.zip",
        [
            _document(
                inn=inn,
                document_id="MIGRATION-B",
                data_date="01.09.2026",
                total="210.00",
                arrears="210.00",
                penalties="0.00",
                fines="0.00",
            )
        ],
    )
    pilot_c = _zip(
        tmp_path / "migration-pilot-c.zip",
        [
            _document(
                inn=inn,
                document_id="MIGRATION-C",
                data_date="01.09.2026",
                total="220.00",
                arrears="220.00",
                penalties="0.00",
                fines="0.00",
            )
        ],
    )
    xsd = _controlled_xsd(tmp_path / "migration-structure.xsd")
    discovery = _controlled_discovery(
        artifact_date="20260920",
        data_as_of=date(2026, 9, 1),
        actual_until=date(2026, 10, 20),
        discovered_at=RETRIEVED_AT,
    )
    config = pipeline.ControlledLivePilotConfig(
        enabled=True,
        cohort_inns=frozenset({inn}),
    )
    registry = HandlerRegistry()
    with pipeline_db() as session:
        pipeline.register_fns_tax_debt_handler(session, registry)
        pipeline.approve_fns_tax_debt_controlled_live_handler(
            session,
            approved_by="DEV-010 migration test",
            approved_at=RETRIEVED_AT,
        )
        pipeline.register_fns_tax_debt_controlled_live_handler(session, registry)
        pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=baseline,
            artifact_store=tmp_path / "raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=RETRIEVED_AT,
        )
        session.commit()
    WorkerExecutor(
        session_factory=pipeline_db,
        registry=registry,
        worker_id="dev010-migration-baseline",
        process_start_method="fork",
    ).run_once()

    for ordinal, source in enumerate((pilot_b, pilot_c), start=1):
        with pipeline_db() as session:
            pipeline.enqueue_fns_tax_debt_controlled_live_job(
                session,
                source_path=source,
                xsd_path=xsd,
                artifact_store=tmp_path / "raw",
                discovery=discovery,
                config=config,
                retrieved_at=RETRIEVED_AT + timedelta(hours=ordinal),
            )
            session.commit()
        WorkerExecutor(
            session_factory=pipeline_db,
            registry=registry,
            worker_id=f"dev010-migration-pilot-{ordinal}",
            process_start_method="fork",
        ).run_once()

    with pipeline_db() as session:
        assert session.scalar(
            select(func.count()).select_from(CompanyTaxDebtSnapshot).where(
                CompanyTaxDebtSnapshot.company_id == company_id,
                CompanyTaxDebtSnapshot.data_date == date(2026, 9, 1),
            )
        ) == 2
        baseline_generation = session.scalar(
            select(FnsTaxDebtPublicationGeneration).where(
                FnsTaxDebtPublicationGeneration.dataset_id == dataset_id,
                FnsTaxDebtPublicationGeneration.generation == 0,
            )
        )
        pointer_before = session.get(WorkerPublicationState, SOURCE_ID)
        dataset_before = session.get(DataSet, dataset_id)
        assert baseline_generation is not None
        assert pointer_before.active_pointer != baseline_generation.staging_pointer
        assert pointer_before.rollback_pointer != baseline_generation.staging_pointer
        assert pointer_before.generation == 3
        baseline_state = {
            "staging_pointer": baseline_generation.staging_pointer,
            "raw_pointer": baseline_generation.raw_pointer,
            "checksum": baseline_generation.checksum,
            "worker_run_id": baseline_generation.worker_run_id,
            "source_as_of": baseline_generation.source_as_of,
            "retrieved_at": baseline_generation.retrieved_at,
            "data_date": baseline_generation.last_data_date,
            "record_count": baseline_generation.record_count,
            "coverage": baseline_generation.coverage,
        }
        assert dataset_before.last_data_date == baseline_state["data_date"]

    connection = pipeline_db.kw["bind"]
    with Operations.context(MigrationContext.configure(connection)):
        pilot_migration.downgrade()
        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "company_tax_debt_snapshots"
            )
        }
        assert "publication_generation" not in columns
        assert connection.scalar(
            sa.text(
                "SELECT count(*) FROM company_tax_debt_snapshots "
                "WHERE company_id = :company_id AND dataset_id = :dataset_id"
            ),
            {"company_id": company_id, "dataset_id": dataset_id},
        ) == 1
        publication = connection.execute(
            sa.text(
                "SELECT active_pointer, rollback_pointer, generation, "
                "published_by_run_id, validation_metadata "
                "FROM worker_publication_state WHERE source_id = :source_id"
            ),
            {"source_id": SOURCE_ID},
        ).mappings().one()
        dataset = connection.execute(
            sa.text(
                "SELECT source_as_of, retrieved_at, last_data_date, "
                "record_count, coverage FROM data_sets WHERE id = :dataset_id"
            ),
            {"dataset_id": dataset_id},
        ).mappings().one()
        assert publication["active_pointer"] == baseline_state["staging_pointer"]
        assert publication["rollback_pointer"] is None
        assert publication["generation"] == 4
        assert publication["published_by_run_id"] == baseline_state["worker_run_id"]
        assert publication["validation_metadata"]["checksum"] == baseline_state["checksum"]
        assert (
            publication["validation_metadata"]["validation"]["raw_pointer"]
            == baseline_state["raw_pointer"]
        )
        assert publication["validation_metadata"]["validation"]["fact_generation"] == 0
        assert publication["validation_metadata"]["validation"]["query_generation"] == 0
        assert pipeline._file_uri_to_path(publication["active_pointer"]).is_file()
        assert connection.scalar(
            sa.text(
                "SELECT count(*) FROM fns_tax_debt_raw_artifacts "
                "WHERE dataset_id = :dataset_id AND artifact_reference = :raw_pointer"
            ),
            {
                "dataset_id": dataset_id,
                "raw_pointer": baseline_state["raw_pointer"],
            },
        ) == 1
        assert connection.scalar(
            sa.text("SELECT count(*) FROM worker_runs WHERE id = :worker_run_id"),
            {"worker_run_id": baseline_state["worker_run_id"]},
        ) == 1
        assert dataset["source_as_of"] == baseline_state["source_as_of"]
        assert dataset["retrieved_at"] == baseline_state["retrieved_at"]
        assert dataset["last_data_date"] == baseline_state["data_date"]
        assert dataset["record_count"] == baseline_state["record_count"]
        assert dataset["coverage"] == baseline_state["coverage"]

        pilot_migration.upgrade()
        assert "publication_generation" in {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "company_tax_debt_snapshots"
            )
        }
        pilot_migration.downgrade()
        pilot_migration.upgrade()
