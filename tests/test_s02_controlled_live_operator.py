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

from app.database.base import Base
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
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerPublicationState,
    WorkerRun,
)
from app.providers.fns_tax_debt_provider import TaxDebtDiscovery, TaxDebtOfficialRelease
from app.sources.fns_tax_debt import (
    BASELINE_HANDLER_VERSION,
    CONTROLLED_LIVE_HANDLER_VERSION,
    CONTROLLED_LIVE_PILOT_ENABLED,
    MASS_INGESTION_ENABLED,
    OFFICIAL_SOURCE_PAGE,
    PILOT_ENVIRONMENT,
    SOURCE_ID,
)
from app.worker import execution as worker_execution
from app.worker.execution import WorkerExecutor, create_job
from app.worker.registry import HandlerRegistry
from scripts import run_s02_controlled_live as operator


NOW = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
SOURCE_AS_OF = datetime(2026, 8, 25, tzinfo=timezone.utc)
DATA_AS_OF = date(2026, 8, 1)
ACTUAL_UNTIL = date(2026, 9, 25)
SECRET_CANARIES = (
    "qa-super-secret-password",
    "postgresql://user:SUPERSECRET@host/db",
    "https://example.invalid/?token=SUPERSECRET",
    "/tmp/SUPERSECRET/file.json",
)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


def _valid_inn(seed: int) -> str:
    base = f"{seed % 1_000_000_000:09d}"
    digits = [int(char) for char in base]
    check = (
        sum(
            value * weight
            for value, weight in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))
        )
        % 11
        % 10
    )
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


def _zip_many(path: Path, records: list[tuple[str, str]]) -> Path:
    documents = "".join(
        f"""
        <Документ ИдДок="DEBT-{ordinal}" ДатаДок="25.08.2026"
          ДатаСост="01.08.2026">
          <СведНП ИННЮЛ="{inn}" НаимОрг="ООО {ordinal}" />
          <СведНедоим НаимНалог="Налог на прибыль" СумНедНалог="{amount}"
            СумПени="0.00" СумШтраф="0.00" ОбщСумНедоим="{amount}" />
        </Документ>
        """
        for ordinal, (inn, amount) in enumerate(records, start=1)
    )
    xml = (
        '<Файл ВерсФорм="4.01" ИдФайл="fixture" ТипИнф="ОТКРДАННЫЕ6" '
        f'КолДок="{len(records)}">{documents}</Файл>'
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


@pytest.fixture
def committed_operator_db():
    """Real transactions for the guard/claim snapshot integration test."""

    schema = f"dev013_{uuid4().hex}"
    quoted_schema = engine.dialect.identifier_preparer.quote(schema)
    assert engine.url.database != "kontragent"
    with engine.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
    isolated_engine = sa.create_engine(
        engine.url, pool_pre_ping=True
    ).execution_options(
        schema_translate_map={None: schema},
    )
    try:
        Base.metadata.create_all(isolated_engine)
        with engine.begin() as connection:
            sa.Sequence(
                worker_execution.WORKER_FENCING_SEQUENCE.name,
                schema=schema,
            ).create(connection)
        factory = sessionmaker(
            bind=isolated_engine,
            autoflush=False,
            expire_on_commit=False,
        )
        yield factory
    finally:
        isolated_engine.dispose()
        with engine.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA {quoted_schema} CASCADE"))


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
        pointer = session.get(WorkerPublicationState, SOURCE_ID)
        run = session.get(WorkerRun, pointer.published_by_run_id)
        validation = dict(pointer.validation_metadata["validation"])
        artifact = session.scalar(
            select(FnsTaxDebtRawArtifact).where(
                FnsTaxDebtRawArtifact.dataset_id == dataset.id,
                FnsTaxDebtRawArtifact.artifact_reference == validation["raw_pointer"],
            )
        )
        session.flush()
        session.add(
            FnsTaxDebtPublicationGeneration(
                dataset_id=dataset.id,
                artifact_id=artifact.id,
                worker_run_id=run.id,
                generation=0,
                publication_scope="baseline",
                status="baseline",
                staging_pointer=pointer.active_pointer,
                raw_pointer=artifact.artifact_reference,
                checksum=artifact.sha256,
                source_as_of=dataset.source_as_of,
                retrieved_at=dataset.retrieved_at,
                official_actual_until=ACTUAL_UNTIL,
                last_data_date=dataset.last_data_date,
                record_count=dataset.record_count,
                coverage=dict(dataset.coverage),
                counters=operator._worker_run_counters(run),
                validation_metadata=validation,
                dataset_metadata=operator._dataset_metadata_snapshot(dataset),
                published_at=dataset.published_at,
            )
        )
        session.commit()
    return company_id


def _seed_baseline_prerequisites(factory, *inns: str) -> dict[str, int]:
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
        if (
            session.scalar(select(DataSet).where(DataSet.code == "fns_tax_debt"))
            is None
        ):
            session.add(
                DataSet(
                    source_id=source.id,
                    code="fns_tax_debt",
                    name="ФНС: Налоговая задолженность",
                    domain="tax_debt",
                    update_mode="bulk",
                    data_format="xml",
                    priority=10,
                    enabled=False,
                )
            )
        result = {}
        for inn in inns:
            company = Company(
                inn=inn,
                name=f"Official Master {inn}",
                entity_type="legal",
                source="fns_egrul",
            )
            session.add(company)
            session.flush()
            result[inn] = company.id
        session.commit()
        return result


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "artifact_final_url",
            "https://www.nalog.gov.ru/redirect/data-20260825-structure-20181201.zip",
        ),
        (
            "xsd_final_url",
            "https://www.nalog.gov.ru/redirect/structure-20181201.xsd",
        ),
        (
            "artifact_final_url",
            "https://file.nalog.ru/opendata/other/data-20260825-structure-20181201.zip",
        ),
    ],
)
def test_manifest_rejects_untrusted_final_transport_coordinates(
    source_files, field, value
):
    _write_manifest(
        source_files.manifest,
        source_files.artifact,
        source_files.xsd,
        **{field: value},
    )
    with pytest.raises(operator.OperatorError) as caught:
        operator.load_source_package(source_files.manifest)
    assert caught.value.code == "MANIFEST_INVALID"


@pytest.mark.parametrize(
    "field",
    [
        "artifact_requested_url",
        "artifact_final_url",
        "xsd_requested_url",
        "xsd_final_url",
    ],
)
@pytest.mark.parametrize(
    "suffix",
    ["?token=SUPERSECRET", "#SUPERSECRET"],
)
def test_manifest_download_coordinates_reject_query_and_fragment(
    source_files, field, suffix
):
    package = json.loads(source_files.manifest.read_text(encoding="utf-8"))
    package[field] += suffix
    source_files.manifest.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(operator.OperatorError) as caught:
        operator.load_source_package(source_files.manifest)
    assert caught.value.code == "MANIFEST_INVALID"


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
    assert report["baseline_generation"] == 0
    assert report["mass_ingestion_enabled"] is False
    assert before == after


def _baseline_chain(session):
    dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_tax_debt"))
    pointer = session.get(WorkerPublicationState, SOURCE_ID)
    run = session.get(WorkerRun, pointer.published_by_run_id)
    artifact = session.scalar(
        select(FnsTaxDebtRawArtifact).where(
            FnsTaxDebtRawArtifact.dataset_id == dataset.id,
            FnsTaxDebtRawArtifact.artifact_reference
            == pointer.validation_metadata["validation"]["raw_pointer"],
        )
    )
    generation = session.scalar(
        select(FnsTaxDebtPublicationGeneration).where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.generation == 0,
        )
    )
    return dataset, pointer, run, artifact, generation


def _add_alternate_artifact(session, dataset, run, *, reference, checksum):
    artifact = FnsTaxDebtRawArtifact(
        dataset_id=dataset.id,
        first_worker_run_id=run.id,
        sha256=checksum,
        artifact_reference=reference,
        original_file_name="alternate.zip",
        media_type="application/zip",
        size_bytes=1,
        manifest={"fixture": True},
        source_as_of=dataset.source_as_of or SOURCE_AS_OF,
        retrieved_at=dataset.retrieved_at or NOW,
    )
    session.add(artifact)
    session.flush()
    return artifact


BASELINE_FAILURE_CASES = (
    "generation_missing",
    "published_by_run_null",
    "published_by_run_missing",
    "run_not_succeeded",
    "active_pointer_mismatch",
    "validation_raw_pointer_mismatch",
    "pointer_checksum_mismatch",
    "raw_artifact_missing",
    "raw_artifact_wrong_dataset",
    "generation_artifact_mismatch",
    "generation_run_mismatch",
    "generation_raw_pointer_mismatch",
    "generation_checksum_mismatch",
    "generation_source_as_of_mismatch",
    "generation_retrieved_at_mismatch",
    "generation_last_data_date_mismatch",
    "generation_actual_until_mismatch",
    "generation_wrong_scope",
    "generation_wrong_status",
)


@pytest.mark.parametrize("case", BASELINE_FAILURE_CASES)
def test_exact_baseline_chain_adversarial_cases_are_blocked_read_only(
    operator_db, source_files, tmp_path, monkeypatch, case
):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        dataset, pointer, run, artifact, generation = _baseline_chain(session)
        if case == "generation_missing":
            session.delete(generation)
        elif case == "published_by_run_null":
            pointer.published_by_run_id = None
        elif case == "published_by_run_missing":
            original_get = session.get

            def missing_run(entity, identity, *args, **kwargs):
                if entity is WorkerRun and identity == run.id:
                    return None
                return original_get(entity, identity, *args, **kwargs)

            monkeypatch.setattr(session, "get", missing_run)
        elif case == "run_not_succeeded":
            run.status = "failed"
        elif case == "active_pointer_mismatch":
            pointer.active_pointer = "file:///safe/mismatched-staging.json"
        elif case == "validation_raw_pointer_mismatch":
            alternate = _add_alternate_artifact(
                session,
                dataset,
                run,
                reference="file:///safe/alternate-raw.zip",
                checksum="1" * 64,
            )
            pointer.validation_metadata = {
                **dict(pointer.validation_metadata),
                "validation": {
                    **dict(pointer.validation_metadata["validation"]),
                    "raw_pointer": alternate.artifact_reference,
                },
            }
        elif case == "pointer_checksum_mismatch":
            pointer.validation_metadata = {
                **dict(pointer.validation_metadata),
                "checksum": "2" * 64,
            }
        elif case == "raw_artifact_missing":
            pointer.validation_metadata = {
                **dict(pointer.validation_metadata),
                "validation": {
                    **dict(pointer.validation_metadata["validation"]),
                    "raw_pointer": "file:///safe/missing-raw.zip",
                },
            }
        elif case == "raw_artifact_wrong_dataset":
            other_dataset = DataSet(
                source_id=dataset.source_id,
                code=f"other-{uuid4()}",
                name="Other dataset",
                domain="tax_debt",
                update_mode="bulk",
                data_format="xml",
                priority=100,
                enabled=False,
            )
            session.add(other_dataset)
            session.flush()
            alternate = _add_alternate_artifact(
                session,
                other_dataset,
                run,
                reference="file:///safe/wrong-dataset.zip",
                checksum="3" * 64,
            )
            pointer.validation_metadata = {
                **dict(pointer.validation_metadata),
                "validation": {
                    **dict(pointer.validation_metadata["validation"]),
                    "raw_pointer": alternate.artifact_reference,
                },
                "checksum": alternate.sha256,
            }
        elif case == "generation_artifact_mismatch":
            alternate = _add_alternate_artifact(
                session,
                dataset,
                run,
                reference="file:///safe/generation-artifact.zip",
                checksum="4" * 64,
            )
            generation.artifact_id = alternate.id
        elif case == "generation_run_mismatch":
            other_job = create_job(
                session,
                source_id=SOURCE_ID,
                job_type="fns_tax_debt_fixture",
                handler_version=run.handler_version,
                idempotency_key=f"other-baseline-{uuid4()}",
                now=NOW,
            ).job
            other_run = WorkerRun(
                job_id=other_job.id,
                attempt_no=1,
                started_at=NOW,
                finished_at=NOW,
                status="succeeded",
                worker_id="other-baseline",
                fencing_token=999,
                handler_version=run.handler_version,
                current_stage="completed",
                errors=[],
                checksum_metadata={},
                heartbeat_at=NOW,
                records_seen=run.records_seen,
                records_written=run.records_written,
                records_rejected=run.records_rejected,
                records_duplicated=run.records_duplicated,
                records_published=run.records_published,
                retryable=False,
            )
            session.add(other_run)
            session.flush()
            generation.worker_run_id = other_run.id
        elif case == "generation_raw_pointer_mismatch":
            generation.raw_pointer = "file:///safe/wrong-generation-raw.zip"
        elif case == "generation_checksum_mismatch":
            generation.checksum = "5" * 64
        elif case == "generation_source_as_of_mismatch":
            generation.source_as_of += timedelta(days=1)
        elif case == "generation_retrieved_at_mismatch":
            generation.retrieved_at += timedelta(seconds=1)
        elif case == "generation_last_data_date_mismatch":
            generation.last_data_date += timedelta(days=1)
        elif case == "generation_actual_until_mismatch":
            generation.official_actual_until += timedelta(days=1)
        elif case == "generation_wrong_scope":
            generation.publication_scope = "pilot"
        elif case == "generation_wrong_status":
            generation.status = "superseded"
        session.commit()

        with pytest.raises(operator.OperatorError) as caught:
            operator.database_readiness(session, cohort=(source_files.inn,))
        assert caught.value.code == "BASELINE_NOT_READY"
        assert not session.new
        assert not session.dirty
        assert not session.deleted


def test_valid_exact_baseline_chain_is_ready_without_pilot_state(
    operator_db, source_files, tmp_path
):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        assert session.get(FnsTaxDebtPilotState, SOURCE_ID) is None
        readiness = operator.database_readiness(session, cohort=(source_files.inn,))
    assert readiness["baseline_generation"] == 0
    assert readiness["baseline_metadata_captured"] is True
    assert readiness["pilot_generation"] is None


def test_existing_pilot_state_cannot_contradict_baseline_generation_zero(
    operator_db, source_files, tmp_path
):
    _seed_baseline(operator_db, tmp_path, source_files.inn)
    with operator_db() as session:
        dataset, _pointer, _run, artifact, _generation = _baseline_chain(session)
        session.add(
            FnsTaxDebtPilotState(
                source_id=SOURCE_ID,
                dataset_id=dataset.id,
                pilot_environment=PILOT_ENVIRONMENT,
                enabled=True,
                cohort_inns=[source_files.inn],
                active_raw_pointer=artifact.artifact_reference,
                active_checksum="9" * 64,
                active_source_as_of=dataset.source_as_of,
                active_retrieved_at=dataset.retrieved_at,
                generation=0,
                baseline_generation=0,
                normalized_generation=0,
                fact_generation=0,
                query_generation=0,
                baseline_data_date=dataset.last_data_date,
                counters={},
                freshness="unknown",
                errors=[],
            )
        )
        session.commit()
        with pytest.raises(operator.OperatorError) as caught:
            operator.database_readiness(session, cohort=(source_files.inn,))
    assert caught.value.code == "BASELINE_NOT_READY"
    assert "pilot_checksum" in caught.value.details["failed_checks"]


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


def _preflight_cli_args(source_files, **changes):
    values = {
        "source_package": str(source_files.manifest),
        "artifact": str(source_files.artifact),
        "xsd": str(source_files.xsd),
        "cohort": str(source_files.cohort),
        "expected_main_sha": operator.resolve_runtime_sha(),
    }
    values.update(changes)
    return [
        "preflight",
        "--source-package",
        values["source_package"],
        "--artifact",
        values["artifact"],
        "--xsd",
        values["xsd"],
        "--cohort",
        values["cohort"],
        "--expected-main-sha",
        values["expected_main_sha"],
    ]


def _assert_secret_safe_cli(capsys, expected_code):
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    payload = json.loads(captured.out)
    assert payload["error"] == expected_code
    for canary in SECRET_CANARIES:
        assert canary not in rendered


def test_missing_and_malformed_manifest_errors_are_secret_safe(
    source_files, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        operator, "session_factory", lambda _database_url: lambda: _FailingSession()
    )
    missing = "/tmp/qa-super-secret-password/missing-manifest.json"
    assert (
        operator.main(_preflight_cli_args(source_files, source_package=missing)) == 33
    )
    _assert_secret_safe_cli(capsys, "MANIFEST_INVALID")

    secret_dir = tmp_path / "qa-super-secret-password"
    secret_dir.mkdir()
    malformed = secret_dir / "manifest.json"
    malformed.write_text(SECRET_CANARIES[1], encoding="utf-8")
    assert (
        operator.main(_preflight_cli_args(source_files, source_package=str(malformed)))
        == 33
    )
    _assert_secret_safe_cli(capsys, "MANIFEST_INVALID")


def test_missing_and_malformed_cohort_errors_are_secret_safe(
    source_files, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        operator, "session_factory", lambda _database_url: lambda: _FailingSession()
    )
    missing = "/tmp/SUPERSECRET/file.json"
    assert operator.main(_preflight_cli_args(source_files, cohort=missing)) == 23
    _assert_secret_safe_cli(capsys, "COHORT_INVALID")

    malformed = tmp_path / "qa-super-secret-password-cohort.json"
    malformed.write_text(SECRET_CANARIES[2], encoding="utf-8")
    assert operator.main(_preflight_cli_args(source_files, cohort=str(malformed))) == 23
    _assert_secret_safe_cli(capsys, "COHORT_INVALID")


@pytest.mark.parametrize(
    ("kind", "missing_path"),
    [
        ("artifact", "/tmp/SUPERSECRET/data-20260825-structure-20181201.zip"),
        ("xsd", "/tmp/SUPERSECRET/structure-20181201.xsd"),
    ],
)
def test_missing_artifact_and_xsd_errors_are_secret_safe(
    source_files, monkeypatch, capsys, kind, missing_path
):
    monkeypatch.setattr(
        operator, "session_factory", lambda _database_url: lambda: _FailingSession()
    )
    assert (
        operator.main(_preflight_cli_args(source_files, **{kind: missing_path})) == 22
    )
    _assert_secret_safe_cli(capsys, "CHECKSUM_MISMATCH")


@pytest.mark.parametrize("hash_kind", ["artifact", "xsd"])
def test_hash_rediscovery_and_provider_failures_are_secret_safe(
    source_files, monkeypatch, capsys, hash_kind
):
    monkeypatch.setattr(
        operator, "session_factory", lambda _database_url: lambda: _FailingSession()
    )

    def hash_failure(path):
        if Path(path) == getattr(source_files, hash_kind):
            raise OSError(SECRET_CANARIES[0])
        return pipeline.calculate_sha256(path)

    monkeypatch.setattr(operator, "calculate_sha256", hash_failure)
    assert operator.main(_preflight_cli_args(source_files)) == 22
    _assert_secret_safe_cli(capsys, "CHECKSUM_MISMATCH")
    monkeypatch.setattr(operator, "calculate_sha256", pipeline.calculate_sha256)

    class FailingDiscoveryClient:
        def discover(self, **_kwargs):
            raise RuntimeError(SECRET_CANARIES[2])

    monkeypatch.setattr(operator, "FnsTaxDebtOfficialClient", FailingDiscoveryClient)
    assert operator.main(_preflight_cli_args(source_files)) == 20
    _assert_secret_safe_cli(capsys, "SOURCE_PACKAGE_CHANGED")

    monkeypatch.setattr(
        operator,
        "validate_tax_debt_release",
        lambda _release: (_ for _ in ()).throw(RuntimeError(SECRET_CANARIES[1])),
    )
    assert operator.main(_preflight_cli_args(source_files)) == 33
    _assert_secret_safe_cli(capsys, "MANIFEST_INVALID")


def test_cli_boundary_removes_raw_database_status_and_arbitrary_error_material(
    monkeypatch, capsys
):
    args = SimpleNamespace(
        command="status", database_url=SECRET_CANARIES[1], job_id=None
    )
    monkeypatch.setattr(operator, "parse_arguments", lambda _argv=None: args)
    monkeypatch.setattr(operator, "session_factory", lambda _database_url: object())

    for raw_error in (
        RuntimeError(SECRET_CANARIES[1]),
        operator.OperatorError(
            "OPERATOR_ERROR",
            SECRET_CANARIES[2],
            details={
                "unsafe": SECRET_CANARIES[3],
                "kind": SECRET_CANARIES[0],
                "failed_checks": [SECRET_CANARIES[0]],
            },
        ),
    ):

        def fail(_args, _factory, error=raw_error):
            raise error

        monkeypatch.setitem(operator.COMMANDS, "status", fail)
        assert operator.main([]) == 40
        _assert_secret_safe_cli(capsys, "OPERATOR_ERROR")


class _FailingSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def rollback(self):
        return None


def test_evidence_and_rollback_wrappers_do_not_copy_provider_errors(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError(SECRET_CANARIES[0])

    monkeypatch.setattr(operator, "calculate_s02_vertical_slice_from_persisted", fail)
    with pytest.raises(operator.OperatorError) as evidence:
        operator.command_evidence(
            SimpleNamespace(company_id=1), lambda: _FailingSession()
        )
    assert evidence.value.code == "EVIDENCE_NOT_AVAILABLE"
    assert SECRET_CANARIES[0] not in str(evidence.value)

    monkeypatch.setattr(operator, "check_main_sha", lambda **_kwargs: "0" * 40)
    monkeypatch.setattr(
        operator,
        "_generation_state",
        lambda _session: {
            "worker_generation": 1,
            "rollback_pointer_identity": "0" * 64,
        },
    )
    monkeypatch.setattr(operator, "rollback_fns_tax_debt_generation", fail)
    with pytest.raises(operator.OperatorError) as rollback:
        operator.command_rollback(
            SimpleNamespace(
                confirm_rollback=operator.ROLLBACK_TOKEN,
                expected_main_sha="0" * 40,
                expected_generation=1,
            ),
            lambda: _FailingSession(),
        )
    assert rollback.value.code == "ROLLBACK_NOT_AVAILABLE"
    assert SECRET_CANARIES[0] not in str(rollback.value)


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
            operator.queue_guard(
                session, expected_job_id=expected.id, now=NOW + timedelta(seconds=2)
            )
        assert blocked.value.code == "QUEUE_NOT_EXCLUSIVE"
        with pytest.raises(operator.OperatorError) as wrong:
            operator.queue_guard(
                session, expected_job_id=earlier.id, now=NOW + timedelta(seconds=2)
            )
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


def test_prepare_baseline_is_bounded_idempotent_and_supports_same_release_run_a(
    committed_operator_db, source_files, tmp_path, monkeypatch
):
    outside_inn = _valid_inn(uuid4().int)
    _zip_many(
        source_files.artifact,
        [(source_files.inn, "225.00"), (outside_inn, "900.00")],
    )
    _write_manifest(source_files.manifest, source_files.artifact, source_files.xsd)
    company_ids = _seed_baseline_prerequisites(
        committed_operator_db,
        source_files.inn,
        outside_inn,
    )
    monkeypatch.setattr(
        operator, "FnsTaxDebtOfficialClient", lambda: _DiscoveryClient()
    )
    monkeypatch.setattr(operator, "_utc_now", lambda: NOW)
    monkeypatch.setattr(worker_execution, "utc_now", lambda: NOW)
    monkeypatch.setattr(pipeline, "datetime", _FrozenDateTime)
    real_executor = operator.WorkerExecutor
    monkeypatch.setattr(
        operator,
        "WorkerExecutor",
        lambda **kwargs: real_executor(process_start_method="fork", **kwargs),
    )
    base = _preflight_args(source_files, tmp_path)
    prepare_args = SimpleNamespace(
        **vars(base),
        artifact_store=tmp_path / "official-raw",
        worker_id="s02-baseline-test",
        timeout_seconds=3600,
        confirm_prepare_baseline=operator.BASELINE_PREPARE_TOKEN,
    )

    with pytest.raises(operator.OperatorError) as missing_approval:
        operator.command_prepare_baseline(prepare_args, committed_operator_db)
    assert missing_approval.value.code == "DURABLE_APPROVAL_MISSING"

    approval = operator.command_approve_baseline(
        SimpleNamespace(
            **vars(base),
            approved_by="S02 baseline integration",
            approved_at=NOW.isoformat(),
            confirm_baseline_approval=operator.BASELINE_APPROVE_TOKEN,
        ),
        committed_operator_db,
    )
    assert approval["handler_version"] == BASELINE_HANDLER_VERSION
    with committed_operator_db() as session:
        assert session.scalar(select(func.count()).select_from(WorkerJob)) == 0

    prepared = operator.command_prepare_baseline(prepare_args, committed_operator_db)
    repeated = operator.command_prepare_baseline(prepare_args, committed_operator_db)
    assert prepared["status"] == "BASELINE_READY"
    assert prepared["created"] is True
    assert prepared["baseline_generation"] == 0
    assert prepared["outside_cohort_facts"] == 0
    assert repeated["created"] is False
    assert repeated["job_id"] == prepared["job_id"]
    assert repeated["run_id"] == prepared["run_id"]

    with committed_operator_db() as session:
        baseline_job = session.get(WorkerJob, prepared["job_id"])
        baseline_run = session.get(WorkerRun, prepared["run_id"])
        generation = session.scalar(
            select(FnsTaxDebtPublicationGeneration).where(
                FnsTaxDebtPublicationGeneration.generation == 0
            )
        )
        facts = session.scalars(
            select(CompanyTaxDebtSnapshot).where(
                CompanyTaxDebtSnapshot.publication_generation == 0
            )
        ).all()
        assert baseline_job.job_type == "fns_tax_debt_baseline"
        assert baseline_job.status == "succeeded"
        assert baseline_run.status == "succeeded"
        assert generation.publication_scope == "baseline"
        assert generation.coverage["cohort_inns"] == [source_files.inn]
        assert {row.company_id for row in facts} == {company_ids[source_files.inn]}

    preflight = operator.command_preflight(base, committed_operator_db)
    assert preflight["status"] == "READY"
    operator.command_approve(
        SimpleNamespace(
            **vars(base),
            approved_by="S02 Run A integration",
            approved_at=NOW.isoformat(),
            confirm_controlled_live=operator.APPROVE_TOKEN,
        ),
        committed_operator_db,
    )
    enqueued = operator.command_enqueue(
        SimpleNamespace(
            **vars(base),
            artifact_store=tmp_path / "official-raw",
            retrieved_at=NOW.isoformat(),
            timeout_seconds=3600,
            confirm_enqueue=operator.ENQUEUE_TOKEN,
        ),
        committed_operator_db,
    )
    run = operator.command_run_once(
        SimpleNamespace(
            job_id=str(enqueued["job_id"]),
            worker_id="s02-run-a-test",
            expected_main_sha=operator.resolve_runtime_sha(),
            confirm_run=operator.RUN_TOKEN,
        ),
        committed_operator_db,
    )
    assert run["status"] == "succeeded"

    with committed_operator_db() as session:
        snapshots = session.scalars(
            select(CompanyTaxDebtSnapshot)
            .where(CompanyTaxDebtSnapshot.company_id == company_ids[source_files.inn])
            .order_by(CompanyTaxDebtSnapshot.publication_generation)
        ).all()
        assert [row.publication_generation for row in snapshots] == [0, 1]
        assert snapshots[0].normalized_record_id is not None
        assert snapshots[1].normalized_record_id is None
        assert (
            session.scalar(select(func.count()).select_from(FnsTaxDebtNormalizedRecord))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(CompanyTaxDebtSnapshot)
                .where(CompanyTaxDebtSnapshot.company_id == company_ids[outside_inn])
            )
            == 0
        )


def test_baseline_cohort_is_capped_at_40(source_files, tmp_path):
    cohort = tmp_path / "baseline-41.json"
    cohort.write_text(
        json.dumps({"inns": [_valid_inn(index) for index in range(1, 42)]}),
        encoding="utf-8",
    )
    with pytest.raises(operator.OperatorError) as caught:
        operator.build_baseline_preparation_report(
            None,
            source_package=source_files.manifest,
            artifact=source_files.artifact,
            xsd=source_files.xsd,
            cohort_path=cohort,
            expected_main_sha=operator.resolve_runtime_sha(),
            now=NOW,
        )
    assert caught.value.code == "COHORT_INVALID"


def test_disposable_postgres_operator_flow(
    committed_operator_db, source_files, tmp_path, monkeypatch
):
    company_id = _seed_baseline(committed_operator_db, tmp_path, source_files.inn)
    monkeypatch.setattr(
        operator, "FnsTaxDebtOfficialClient", lambda: _DiscoveryClient()
    )
    monkeypatch.setattr(operator, "_utc_now", lambda: NOW)
    monkeypatch.setattr(worker_execution, "utc_now", lambda: NOW)
    monkeypatch.setattr(pipeline, "datetime", _FrozenDateTime)
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
        assert (
            session.scalar(select(func.count()).select_from(WorkerJob)) == jobs_before
        )
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
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkerRun)
                .where(WorkerRun.job_id == first["job_id"])
            )
            == 0
        )

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
