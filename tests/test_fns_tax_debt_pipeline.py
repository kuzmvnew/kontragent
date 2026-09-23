from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_tax_debt_pipeline as pipeline
from app.models.company import Company
from app.models.source import DataSet, DataSource
from app.models.tax_debt import (
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtRawArtifact,
)
from app.models.worker import WorkerJob, WorkerPublicationState, WorkerRawManifest
from app.services.tax_debt_service import prepare_tax_debt_public_projection
from app.sources.fns_tax_debt import (
    CONTROLLED_LIVE_PILOT_ENABLED,
    FACT_CODE,
    FNS_TAX_DEBT_SOURCE_CONTRACT,
    MASS_INGESTION_ENABLED,
    SOURCE_ID,
)
from app.worker.contracts import HandlerContext
from app.worker.errors import (
    LegalBlockError,
    TemporaryInfrastructureError,
    WorkerTimeoutError,
)
from app.worker.execution import RetryPolicy
from app.worker.execution import WorkerExecutor, create_job, register_handler
from app.worker.registry import HandlerRegistry


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


def test_controlled_live_pilot_is_gated_until_qa(tmp_path):
    source = _zip(tmp_path / "fixture.zip", [_document()])

    with pytest.raises(LegalBlockError, match="QA approval"):
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


def test_transient_publication_failure_rolls_back_domain_rows_and_schedules_retry(
    pipeline_db,
    tmp_path,
):
    inn = _valid_inn(uuid4().int)
    dataset_id, company_id = _ensure_dataset_and_company(pipeline_db, inn=inn)
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
        assert job.status == "retry_scheduled"
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
        ) == 0
