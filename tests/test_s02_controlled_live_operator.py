from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
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
from app.models.worker import WorkerHandlerRegistration, WorkerJob, WorkerRun
from app.providers.fns_tax_debt_provider import TaxDebtDiscovery, TaxDebtOfficialRelease
from app.sources.fns_tax_debt import (
    CONTROLLED_LIVE_HANDLER_VERSION,
    CONTROLLED_LIVE_PILOT_ENABLED,
    MASS_INGESTION_ENABLED,
    OFFICIAL_SOURCE_PAGE,
    SOURCE_ID,
)
from app.worker.execution import WorkerExecutor, create_job
from app.worker.registry import HandlerRegistry
from scripts import run_s02_controlled_live as operator


NOW = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
SOURCE_AS_OF = datetime(2026, 8, 25, tzinfo=timezone.utc)
DATA_AS_OF = date(2026, 8, 1)
ACTUAL_UNTIL = date(2026, 9, 25)


def _valid_inn(seed: int) -> str:
    base = f"{seed % 1_000_000_000:09d}"
    digits = [int(char) for char in base]
    check = sum(
        value * weight
        for value, weight in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))
    ) % 11 % 10
    return base + str(check)


def _zip(path: Path, inn: str, *, amount: str = "125.00") -> Path:
    document = f"""
      <Документ ИдДок="DEBT-{amount}" ДатаДок="25.08.2026" ДатаСост="01.08.2026">
        <СведНП ИННЮЛ="{inn}" НаимОрг="ООО ТЕСТ" />
        <СведНедоим НаимНалог="Налог на прибыль" СумНедНалог="{amount}"
          СумПени="0.00" СумШтраф="0.00" ОбщСумНедоим="{amount}" />
      </Документ>
    """
    xml = (
        '<Файл ВерсФорм="4.01" ИдФайл="fixture" ТипИнф="ОТКРДАННЫЕ6" КолДок="1">'
        + document
        + "</Файл>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("data-1.xml", xml.encode("utf-8"))
    return path


def _xsd(path: Path) -> Path:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="Файл"><xs:complexType><xs:sequence>
            <xs:element name="Документ" minOccurs="1" maxOccurs="unbounded">
              <xs:complexType><xs:sequence>
                <xs:element name="СведНП"><xs:complexType>
                  <xs:attribute name="ИННЮЛ" type="xs:string" use="required"/>
                  <xs:attribute name="НаимОрг" type="xs:string"/>
                </xs:complexType></xs:element>
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
          </xs:complexType></xs:element>
        </xs:schema>""",
        encoding="utf-8",
    )
    return path


def _release() -> TaxDebtOfficialRelease:
    return TaxDebtOfficialRelease(
        discovery_page_url=OFFICIAL_SOURCE_PAGE,
        artifact_url=(
            "https://file.nalog.ru/opendata/7707329152-debtam/"
            "data-20260825-structure-20181201.zip"
        ),
        xsd_url=(
            "https://file.nalog.ru/opendata/7707329152-debtam/structure-20181201.xsd"
        ),
        source_as_of=SOURCE_AS_OF,
        data_as_of=DATA_AS_OF,
        official_actual_until=ACTUAL_UNTIL,
        metadata={},
    )


class _DiscoveryClient:
    def __init__(self, release: TaxDebtOfficialRelease | None = None) -> None:
        self.release = release or _release()

    def discover(self, *, discovered_at, previous=None):
        return TaxDebtDiscovery(
            release=self.release,
            changed=False,
            discovered_at=discovered_at,
        )


def _write_manifest(path: Path, artifact: Path, xsd: Path, **changes) -> Path:
    artifact_hash, artifact_size = pipeline.calculate_sha256(artifact)
    xsd_hash, xsd_size = pipeline.calculate_sha256(xsd)
    release = _release()
    payload = {
        "dataset_id": "7707329152-debtam",
        "official_page": OFFICIAL_SOURCE_PAGE,
        "artifact_requested_url": release.artifact_url,
        "artifact_final_url": release.artifact_url,
        "artifact_filename": artifact.name,
        "artifact_size": artifact_size,
        "artifact_sha256": artifact_hash,
        "xsd_requested_url": release.xsd_url,
        "xsd_final_url": release.xsd_url,
        "xsd_filename": xsd.name,
        "xsd_size": xsd_size,
        "xsd_sha256": xsd_hash,
        "last_modified": "2026-08-25",
        "data_as_of": "2026-08-01",
        "source_as_of": "2026-08-25T00:00:00+00:00",
        "retrieved_at": NOW.isoformat(),
        "official_actual_until": "2026-09-25",
        "structure_version": "20181201",
        "real_xml_version": "4.01",
        "information_type": "ОТКРДАННЫЕ6",
        "validation_result": "PASS",
        "main_sha": operator.resolve_runtime_sha(),
    }
    payload.update(changes)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def operator_db():
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


def _truncate_application_tables() -> None:
    names = [
        name for name in sa.inspect(engine).get_table_names() if name != "alembic_version"
    ]
    if not names:
        return
    quoted = ", ".join(engine.dialect.identifier_preparer.quote(name) for name in names)
    with engine.begin() as connection:
        connection.execute(sa.text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture
def committed_operator_db():
    """Real transactions for the guard/claim snapshot integration test."""

    _truncate_application_tables()
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        _truncate_application_tables()


@pytest.fixture
def source_files(tmp_path):
    inn = _valid_inn(uuid4().int)
    artifact = _zip(
        tmp_path / "data-20260825-structure-20181201.zip", inn, amount="225.00"
    )
    xsd = _xsd(tmp_path / "structure-20181201.xsd")
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"inns": [inn]}), encoding="utf-8")
    manifest = _write_manifest(tmp_path / "manifest.json", artifact, xsd)
    return SimpleNamespace(
        inn=inn, artifact=artifact, xsd=xsd, cohort=cohort, manifest=manifest
    )


def _seed_baseline(factory, tmp_path: Path, inn: str) -> int:
    with factory() as session:
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
        dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_tax_debt"))
        if dataset is None:
            dataset = DataSet(
                source_id=source.id,
                code="fns_tax_debt",
                name="ФНС: Налоговая задолженность",
                domain="tax_debt",
                update_mode="bulk",
                data_format="xml",
                priority=10,
                enabled=False,
            )
            session.add(dataset)
        company = Company(
            inn=inn,
            name=f"DEV-013 {inn}",
            entity_type="legal",
            source="dev013_fixture",
        )
        session.add(company)
        session.commit()
        company_id = company.id
    baseline = _zip(tmp_path / "baseline.zip", inn)
    registry = HandlerRegistry()
    with factory() as session:
        pipeline.register_fns_tax_debt_handler(session, registry)
        pipeline.enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=baseline,
            artifact_store=tmp_path / "baseline-raw",
            source_as_of=SOURCE_AS_OF,
            retrieved_at=NOW - timedelta(days=1),
        )
        session.commit()
    WorkerExecutor(
        session_factory=factory,
        registry=registry,
        worker_id="dev013-baseline",
        process_start_method="fork",
    ).run_once()
    with factory() as session:
        dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_tax_debt"))
        dataset.coverage = {
            **dict(dataset.coverage or {}),
            "official_actual_until": ACTUAL_UNTIL.isoformat(),
        }
        session.commit()
    return company_id


def _preflight_args(files, tmp_path, **changes):
    values = {
        "source_package": files.manifest,
        "artifact": files.artifact,
        "xsd": files.xsd,
        "cohort": files.cohort,
        "expected_main_sha": operator.resolve_runtime_sha(),
        "database_url": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_manifest_local_files_and_cohort_are_canonical(source_files):
    package = operator.load_source_package(source_files.manifest)
    artifact_hash, xsd_hash = operator.verify_local_files(
        package, source_files.artifact, source_files.xsd
    )
    cohort, cohort_hash = operator.load_cohort(source_files.cohort)

    assert artifact_hash == package["artifact_sha256"]
    assert xsd_hash == package["xsd_sha256"]
    assert cohort == (source_files.inn,)
    assert len(cohort_hash) == 64
    assert len(operator.source_package_fingerprint(package)) == 64


@pytest.mark.parametrize("kind", ["artifact", "xsd"])
def test_local_checksum_or_size_drift_is_blocked(source_files, kind):
    package = operator.load_source_package(source_files.manifest)
    path = getattr(source_files, kind)
    path.write_bytes(path.read_bytes() + b"drift")
    with pytest.raises(operator.OperatorError) as caught:
        operator.verify_local_files(package, source_files.artifact, source_files.xsd)
    assert caught.value.code == "CHECKSUM_MISMATCH"


def test_stale_source_package_is_blocked_before_database_access(source_files):
    _write_manifest(
        source_files.manifest,
        source_files.artifact,
        source_files.xsd,
        official_actual_until="2026-09-23",
    )
    with pytest.raises(operator.OperatorError) as caught:
        operator.build_preflight_report(
            None,
            source_package=source_files.manifest,
            artifact=source_files.artifact,
            xsd=source_files.xsd,
            cohort_path=source_files.cohort,
            expected_main_sha=operator.resolve_runtime_sha(),
            now=NOW,
        )
    assert caught.value.code == "SOURCE_PACKAGE_STALE"


def test_changed_official_release_is_blocked(source_files):
    package = operator.load_source_package(source_files.manifest)
    changed = TaxDebtOfficialRelease(
        **{
            **_release().__dict__,
            "artifact_url": _release().artifact_url.replace("20260825", "20260920"),
            "source_as_of": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }
    )
    with pytest.raises(operator.OperatorError) as caught:
        operator.discover_exact_release(
            package, now=NOW, client=_DiscoveryClient(changed)
        )
    assert caught.value.code == "SOURCE_PACKAGE_CHANGED"


@pytest.mark.parametrize(
    "payload",
    [[], ["7707083893", "7707083893"], ["123"], [_valid_inn(i) for i in range(101)]],
)
def test_invalid_duplicate_oversized_or_empty_cohort_is_blocked(tmp_path, payload):
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(operator.OperatorError) as caught:
        operator.load_cohort(path)
    assert caught.value.code == "COHORT_INVALID"


def test_main_sha_mismatch_and_unverified_are_distinct(monkeypatch):
    actual = operator.resolve_runtime_sha()
    with pytest.raises(operator.OperatorError) as mismatch:
        operator.check_main_sha(expected="0" * 40)
    assert mismatch.value.code == "MAIN_SHA_MISMATCH"
    monkeypatch.setattr(operator, "resolve_runtime_sha", lambda: None)
    with pytest.raises(operator.OperatorError) as unverified:
        operator.check_main_sha(expected=actual)
    assert unverified.value.code == "MAIN_SHA_NOT_VERIFIED"


def test_preflight_ready_and_read_only(operator_db, source_files, tmp_path):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        before = session.scalar(select(func.count()).select_from(WorkerJob))
        report, _, _, _ = operator.build_preflight_report(
            session,
            source_package=source_files.manifest,
            artifact=source_files.artifact,
            xsd=source_files.xsd,
            cohort_path=source_files.cohort,
            expected_main_sha=operator.resolve_runtime_sha(),
            now=NOW,
            discovery_client=_DiscoveryClient(),
        )
        session.rollback()
    with operator_db() as session:
        after = session.scalar(select(func.count()).select_from(WorkerJob))
    assert report["status"] == "READY"
    assert report["cohort_count"] == 1
    assert report["mass_ingestion_enabled"] is False
    assert before == after


def test_missing_or_non_legal_master_company_and_baseline_are_blocked(
    operator_db, source_files
):
    with operator_db() as session:
        with pytest.raises(operator.OperatorError) as missing:
            operator.database_readiness(session, cohort=(source_files.inn,))
    assert missing.value.code == "COHORT_INVALID"
    with operator_db() as session:
        source = session.scalar(select(DataSource).where(DataSource.code == "fns"))
        if source is None:
            source = DataSource(
                code="fns", name="FNS", source_type="official", priority=1, enabled=True
            )
            session.add(source)
            session.flush()
        session.add(
            Company(
                inn=source_files.inn,
                name="IP",
                entity_type="individual_entrepreneur",
                source="fixture",
            )
        )
        session.commit()
    with operator_db() as session:
        with pytest.raises(operator.OperatorError) as non_legal:
            operator.database_readiness(session, cohort=(source_files.inn,))
    assert non_legal.value.code == "COHORT_INVALID"


def test_existing_legal_company_without_baseline_is_blocked(operator_db, source_files):
    with operator_db() as session:
        session.add(
            Company(
                inn=source_files.inn,
                name="Legal without baseline",
                entity_type="legal",
                source="fixture",
            )
        )
        session.commit()
        with pytest.raises(operator.OperatorError) as caught:
            operator.database_readiness(session, cohort=(source_files.inn,))
    assert caught.value.code == "BASELINE_NOT_READY"


def test_confirmation_tokens_are_exact(source_files):
    factory = lambda: None
    for function, args, field, token in (
        (
            operator.command_approve,
            _preflight_args(
                source_files,
                source_files.manifest.parent,
                approved_by="operator",
                approved_at=NOW.isoformat(),
                confirm_controlled_live="yes",
            ),
            "confirm_controlled_live",
            operator.APPROVE_TOKEN,
        ),
        (
            operator.command_enqueue,
            _preflight_args(
                source_files,
                source_files.manifest.parent,
                confirm_enqueue="true",
                timeout_seconds=3600,
                retrieved_at=NOW.isoformat(),
                artifact_store=source_files.manifest.parent / "raw",
            ),
            "confirm_enqueue",
            operator.ENQUEUE_TOKEN,
        ),
    ):
        with pytest.raises(operator.OperatorError) as caught:
            function(args, factory)
        assert caught.value.code == "CONFIRMATION_REQUIRED"
        assert token in str(caught.value)


def test_queue_guard_blocks_wrong_job_and_earlier_runnable_job(
    operator_db, source_files, tmp_path
):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        pipeline.approve_fns_tax_debt_controlled_live_handler(
            session, approved_by="queue-test", approved_at=NOW
        )
        earlier = create_job(
            session,
            source_id="OTHER",
            job_type="fixture",
            handler_version="other-v1",
            idempotency_key=f"other-{uuid4()}",
            now=NOW,
        ).job
        expected = create_job(
            session,
            source_id=SOURCE_ID,
            job_type="fns_tax_debt_controlled_live",
            handler_version=CONTROLLED_LIVE_HANDLER_VERSION,
            idempotency_key=f"s02-{uuid4()}",
            now=NOW + timedelta(seconds=1),
        ).job
        session.commit()
        with pytest.raises(operator.OperatorError) as blocked:
            operator.queue_guard(session, expected_job_id=expected.id, now=NOW + timedelta(seconds=2))
        assert blocked.value.code == "QUEUE_NOT_EXCLUSIVE"
        with pytest.raises(operator.OperatorError) as wrong:
            operator.queue_guard(session, expected_job_id=earlier.id, now=NOW + timedelta(seconds=2))
        assert wrong.value.code == "JOB_MISMATCH"


@pytest.mark.parametrize(
    ("source_id", "job_type", "handler_version"),
    [
        ("OTHER", "fns_tax_debt_controlled_live", CONTROLLED_LIVE_HANDLER_VERSION),
        (SOURCE_ID, "other", CONTROLLED_LIVE_HANDLER_VERSION),
        (SOURCE_ID, "fns_tax_debt_controlled_live", "wrong-version"),
    ],
)
def test_queue_guard_rejects_each_job_contract_mismatch(
    operator_db,
    source_files,
    tmp_path,
    source_id,
    job_type,
    handler_version,
):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        pipeline.approve_fns_tax_debt_controlled_live_handler(
            session, approved_by="contract-test", approved_at=NOW
        )
        job = create_job(
            session,
            source_id=source_id,
            job_type=job_type,
            handler_version=handler_version,
            idempotency_key=f"contract-{uuid4()}",
            now=NOW,
        ).job
        session.commit()
        with pytest.raises(operator.OperatorError) as caught:
            operator.queue_guard(session, expected_job_id=job.id, now=NOW)
    assert caught.value.code == "JOB_MISMATCH"


def test_queue_guard_requires_durable_approval(operator_db, source_files, tmp_path):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        job = create_job(
            session,
            source_id=SOURCE_ID,
            job_type="fns_tax_debt_controlled_live",
            handler_version=CONTROLLED_LIVE_HANDLER_VERSION,
            idempotency_key=f"approval-{uuid4()}",
            now=NOW,
        ).job
        session.commit()
        with pytest.raises(operator.OperatorError) as caught:
            operator.queue_guard(session, expected_job_id=job.id, now=NOW)
    assert caught.value.code == "DURABLE_APPROVAL_MISSING"


@pytest.mark.parametrize("timeout", [599, 7201])
def test_enqueue_timeout_override_is_bounded(source_files, timeout):
    args = _preflight_args(
        source_files,
        source_files.manifest.parent,
        confirm_enqueue=operator.ENQUEUE_TOKEN,
        timeout_seconds=timeout,
        retrieved_at=NOW.isoformat(),
        artifact_store=source_files.manifest.parent / "raw",
    )
    with pytest.raises(operator.OperatorError) as caught:
        operator.command_enqueue(args, lambda: None)
    assert caught.value.code == "OPERATOR_ERROR"


def test_disposable_postgres_operator_flow(
    committed_operator_db, source_files, tmp_path, monkeypatch
):
    company_id = _seed_baseline(committed_operator_db, tmp_path, source_files.inn)
    monkeypatch.setattr(operator, "FnsTaxDebtOfficialClient", lambda: _DiscoveryClient())
    monkeypatch.setattr(operator, "_utc_now", lambda: NOW)
    base = _preflight_args(source_files, tmp_path)

    preflight = operator.command_preflight(base, committed_operator_db)
    assert preflight["status"] == "READY"

    with committed_operator_db() as session:
        jobs_before = session.scalar(select(func.count()).select_from(WorkerJob))
    approve_args = SimpleNamespace(
        **vars(base),
        approved_by="DEV-013 integration",
        approved_at=NOW.isoformat(),
        confirm_controlled_live=operator.APPROVE_TOKEN,
    )
    approval = operator.command_approve(approve_args, committed_operator_db)
    assert approval["handler_version"] == CONTROLLED_LIVE_HANDLER_VERSION
    with committed_operator_db() as session:
        assert session.scalar(select(func.count()).select_from(WorkerJob)) == jobs_before
        durable = session.get(
            WorkerHandlerRegistration, (SOURCE_ID, CONTROLLED_LIVE_HANDLER_VERSION)
        )
        assert durable.metadata_json["approved_by"] == "DEV-013 integration"

    enqueue_args = SimpleNamespace(
        **vars(base),
        artifact_store=tmp_path / "controlled-raw",
        retrieved_at=NOW.isoformat(),
        timeout_seconds=3600,
        confirm_enqueue=operator.ENQUEUE_TOKEN,
    )
    first = operator.command_enqueue(enqueue_args, committed_operator_db)
    second = operator.command_enqueue(enqueue_args, committed_operator_db)
    assert first["created"] is True
    assert second["created"] is False
    assert first["job_id"] == second["job_id"]
    with committed_operator_db() as session:
        assert session.scalar(
            select(func.count()).select_from(WorkerRun).where(
                WorkerRun.job_id == first["job_id"]
            )
        ) == 0

    real_executor = operator.WorkerExecutor
    monkeypatch.setattr(
        operator,
        "WorkerExecutor",
        lambda **kwargs: real_executor(process_start_method="fork", **kwargs),
    )
    run = operator.command_run_once(
        SimpleNamespace(
            job_id=str(first["job_id"]),
            worker_id="dev013-integration",
            expected_main_sha=operator.resolve_runtime_sha(),
            confirm_run=operator.RUN_TOKEN,
        ),
        committed_operator_db,
    )
    assert run["status"] == "succeeded"
    assert run["job_id"] == first["job_id"]
    assert run["generation_after"]["worker_generation"] == 2
    with committed_operator_db() as session:
        assert session.scalar(select(func.count()).select_from(WorkerRun)) == 2

    status = operator.command_status(
        SimpleNamespace(job_id=str(first["job_id"])), committed_operator_db
    )
    assert status["selected_job"]["status"] == "succeeded"
    assert status["last_run"]["run_id"] == run["run_id"]
    assert "active_raw_pointer" not in status["monitoring"]
    assert status["monitoring"]["active_raw_pointer_identity"]

    evidence = operator.command_evidence(
        SimpleNamespace(company_id=company_id), committed_operator_db
    )
    assert evidence["status"] == "OK"
    assert evidence["fact"]
    assert "provenance" not in evidence["fact"]

    with pytest.raises(operator.OperatorError) as wrong_generation:
        operator.command_rollback(
            SimpleNamespace(
                expected_generation=999,
                expected_main_sha=operator.resolve_runtime_sha(),
                confirm_rollback=operator.ROLLBACK_TOKEN,
            ),
            committed_operator_db,
        )
    assert wrong_generation.value.code == "ROLLBACK_NOT_AVAILABLE"

    rolled_back = operator.command_rollback(
        SimpleNamespace(
            expected_generation=run["generation_after"]["worker_generation"],
            expected_main_sha=operator.resolve_runtime_sha(),
            confirm_rollback=operator.ROLLBACK_TOKEN,
        ),
        committed_operator_db,
    )
    assert rolled_back["status"] == "ROLLED_BACK"
    assert rolled_back["after"]["worker_generation"] == 3
    assert MASS_INGESTION_ENABLED is False
    assert CONTROLLED_LIVE_PILOT_ENABLED is False


def test_rollback_requires_exact_generation_and_confirmation(operator_db):
    with pytest.raises(operator.OperatorError) as confirmation:
        operator.command_rollback(
            SimpleNamespace(
                expected_generation=1,
                expected_main_sha=operator.resolve_runtime_sha(),
                confirm_rollback="yes",
            ),
            operator_db,
        )
    assert confirmation.value.code == "CONFIRMATION_REQUIRED"
    with pytest.raises(operator.OperatorError) as unavailable:
        operator.command_rollback(
            SimpleNamespace(
                expected_generation=1,
                expected_main_sha=operator.resolve_runtime_sha(),
                confirm_rollback=operator.ROLLBACK_TOKEN,
            ),
            operator_db,
        )
    assert unavailable.value.code == "ROLLBACK_NOT_AVAILABLE"
