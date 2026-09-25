from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_bulk_worker as bulk
from app.ingestion import fns_headcount, fns_msp, fns_tax_regime
from app.models.company import Company
from app.models.headcount import CompanyHeadcount
from app.models.msp import CompanyMspProfile
from app.models.source import DataSet, DataSource
from app.models.tax_regime import CompanyTaxRegimeSnapshot
from app.models.worker import WorkerPublicationState
from app.services import data_readiness_scheduler as scheduler
from app.services import source_service
from app.worker.contracts import (
    ExecutionCounters,
    HandlerResult,
    StagingResult,
    ValidationResult,
)
from scripts import run_data_readiness_scheduler as supervisor


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


@pytest.fixture
def next3_db():
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


def _release(spec, *, actual_until=date(2026, 10, 10), source_date=date(2025, 12, 31)):
    return bulk.FnsRelease(
        source_page_url=spec.source_page_url,
        artifact_url=(
            f"https://data.nalog.ru/opendata/{spec.source_path}/"
            "data-20260925-structure-20200101.zip"
        ),
        xsd_url=(
            f"https://data.nalog.ru/opendata/{spec.source_path}/"
            "structure-20200101.xsd"
        ),
        source_data_date=source_date,
        actual_until=actual_until,
        discovered_at=NOW,
        provenance=f"Данные на {source_date:%d.%m.%Y}",
    )


def _source_and_dataset(session, code):
    source = session.scalar(sa.select(DataSource).where(DataSource.code == "fns"))
    if source is None:
        source = DataSource(
            code="fns",
            name="FNS",
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
            domain=code,
            update_mode="bulk",
            data_format="xml",
            priority=10,
            enabled=False,
        )
        session.add(dataset)
        session.flush()
    return dataset


def test_next3_sources_have_independent_non_empty_scheduler_handlers():
    assert scheduler.HANDLERS["fns_headcount"] is scheduler._enqueue_headcount
    assert scheduler.HANDLERS["fns_msp"] is scheduler._enqueue_msp
    assert scheduler.HANDLERS["fns_tax_regime"] is scheduler._enqueue_tax_regime
    assert fns_tax_regime._family_spec().source_id == "fns_tax_regime"
    assert fns_headcount._worker_spec().check_frequency == "weekly"
    assert fns_headcount._worker_spec().check_interval == timedelta(days=7)
    assert {spec.dataset_code for spec in fns_tax_regime._member_specs().values()} == {
        "fns_snr",
        "fns_snrip",
    }


def test_two_artifact_family_freshness_is_fail_closed_and_conservative():
    legal = _release(
        fns_tax_regime._member_specs()["legal"],
        actual_until=date(2026, 9, 25),
    )
    ip = _release(
        fns_tax_regime._member_specs()["ip"],
        actual_until=date(2026, 10, 10),
        source_date=date(2026, 9, 1),
    )
    bundle = bulk.FnsReleaseBundle({"legal": legal, "ip": ip})

    assert bundle.actual_until == date(2026, 9, 25)
    assert bundle.source_data_date == date(2025, 12, 31)
    assert bulk.release_operational_status(
        bundle.actual_until, now=datetime(2026, 9, 25, 23, 59, tzinfo=timezone.utc)
    ).value == "current"
    assert bulk.release_operational_status(
        bundle.actual_until, now=datetime(2026, 9, 26, tzinfo=timezone.utc)
    ).value == "stale"
    assert bulk.FnsReleaseBundle(
        {
            "legal": legal,
            "ip": bulk.FnsRelease(
                **{**ip.__dict__, "actual_until": None}
            ),
        }
    ).actual_until is None


def test_tax_regime_handler_stages_two_artifacts_into_one_publication(
    tmp_path, monkeypatch
):
    specs = fns_tax_regime._member_specs()
    bundle = bulk.FnsReleaseBundle(
        {name: _release(spec) for name, spec in specs.items()}
    )
    normalized_index = 0

    def fake_stage(spec, _release_value, *, raw_root):
        artifact_dir = raw_root / spec.dataset_code
        artifact_dir.mkdir(parents=True, exist_ok=True)
        zip_path = artifact_dir / "data.zip"
        xsd_path = artifact_dir / "structure.xsd"
        zip_path.write_bytes(spec.dataset_code.encode())
        xsd_path.write_bytes(b"xsd")
        checksum, size = bulk._hash_file(zip_path)
        return zip_path, xsd_path, {
            "artifact_sha256": checksum,
            "artifact_size": size,
            "xsd_sha256": bulk._hash_file(xsd_path)[0],
        }

    def fake_normalize(zip_path, *, xsd_path, iterator, postprocess):
        nonlocal normalized_index
        assert xsd_path.is_file()
        assert callable(iterator)
        assert postprocess is fns_tax_regime.coalesce_tax_regime_records
        normalized_index += 1
        path = zip_path.parent / "normalized.jsonl"
        path.write_text(json.dumps({"member": zip_path.parent.name}) + "\n")
        checksum, _ = bulk._hash_file(path)
        return path, checksum, {
            "records_seen": normalized_index,
            "records_valid": normalized_index,
            "records_rejected": 0,
            "source_data_date": bundle.releases[
                "legal" if zip_path.parent.name == "fns_snr" else "ip"
            ].source_data_date.isoformat(),
        }

    monkeypatch.setattr(bulk, "stage_release", fake_stage)
    monkeypatch.setattr(bulk, "normalize_release", fake_normalize)
    context = SimpleNamespace(
        schedule_metadata={**bundle.as_metadata(), "raw_root": str(tmp_path)},
        ensure_active=lambda **_kwargs: None,
        heartbeat=lambda: None,
        report_counters=lambda _counters: None,
    )

    result = fns_tax_regime.fns_tax_regime_worker_handler(context)

    assert len(result.raw_artifacts) == 2
    assert result.staging_result is not None
    assert result.staging_result.validation.metadata["release_identity"] == bundle.identity
    assert result.counters.records_seen == 3
    descriptor = json.loads(
        bulk._file_path(result.staging_result.staging_pointer).read_text()
    )
    assert set(descriptor["members"]) == {"legal", "ip"}
    assert result.checksum_metadata["normalized_bundle_sha256"] == bulk._hash_file(
        bulk._file_path(result.staging_result.staging_pointer)
    )[0]


def test_postgresql_scoped_family_registry_upsert_preserves_child_schedules(
    next3_db, monkeypatch
):
    with next3_db() as session:
        family = _source_and_dataset(session, "fns_tax_regime")
        legal = _source_and_dataset(session, "fns_snr")
        ip = _source_and_dataset(session, "fns_snrip")
        legal.enabled = False
        ip.enabled = False
        family_id = family.id
        session.delete(family)
        session.flush()

        monkeypatch.setattr(source_service, "get_session", next3_db)
        source_service.ensure_default_dataset("fns_tax_regime")

        restored = session.scalar(
            sa.select(DataSet).where(DataSet.code == "fns_tax_regime")
        )
        session.refresh(legal)
        session.refresh(ip)
        assert restored is not None
        assert restored.id != family_id
        assert restored.enabled is False
        assert legal.enabled is False
        assert ip.enabled is False


def test_activation_ensures_only_requested_new_control_dataset(monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_default_dataset",
        lambda code: calls.append(("default", code)),
    )
    monkeypatch.setattr(
        supervisor,
        "ensure_cbr_warning_list_dataset",
        lambda: calls.append(("cbr", "cbr_warning_list")),
    )

    supervisor.ensure_activation_datasets(["fns_tax_regime", "fns_headcount"])

    assert calls == [("default", "fns_tax_regime")]


def test_snrip_duplicate_inn_rows_union_regimes_and_preserve_provenance(tmp_path):
    source = tmp_path / "snrip-normalized.jsonl"
    rows = [
        {
            "inn": "345907922962",
            "entity_type": "individual_entrepreneur",
            "dataset_code": "fns_snrip",
            "data_date": "2026-09-01",
            "ogrn": "324940100014240",
            "source_document_id": "DOC-B",
            "source_document_date": "2026-09-10",
            "regime_codes": ["psn"],
            "unknown_codes": ["9"],
        },
        {
            "inn": "345907922962",
            "entity_type": "individual_entrepreneur",
            "dataset_code": "fns_snrip",
            "data_date": "2026-09-01",
            "ogrn": "324940100014240",
            "source_document_id": "DOC-A",
            "source_document_date": "2026-09-09",
            "regime_codes": ["usn", "npd"],
            "unknown_codes": [],
        },
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in reversed(rows)))

    output, metadata = fns_tax_regime.coalesce_tax_regime_records(source)
    record = json.loads(output.read_text())
    forward = tmp_path / "snrip-forward.jsonl"
    forward.write_text("".join(json.dumps(row) + "\n" for row in rows))
    forward_output, _ = fns_tax_regime.coalesce_tax_regime_records(forward)

    assert metadata == {"normalized_unique_inns": 1, "duplicate_inn_rows": 1}
    assert output.read_bytes() == forward_output.read_bytes()
    assert record["regime_codes"] == ["npd", "psn", "usn"]
    assert record["unknown_codes"] == ["9"]
    assert record["source_document_id"] == "DOC-A"
    assert record["source_documents"] == [
        {
            "source_document_id": "DOC-A",
            "source_document_date": "2026-09-09",
        },
        {
            "source_document_id": "DOC-B",
            "source_document_date": "2026-09-10",
        },
    ]


def test_snrip_duplicate_inn_mixed_dates_fail_closed(tmp_path):
    source = tmp_path / "snrip-mixed-date.jsonl"
    base = {
        "inn": "345907922962",
        "entity_type": "individual_entrepreneur",
        "dataset_code": "fns_snrip",
        "source_document_id": "DOC",
        "source_document_date": "2026-09-10",
        "regime_codes": ["usn"],
        "unknown_codes": [],
    }
    source.write_text(
        json.dumps({**base, "data_date": "2026-09-01"})
        + "\n"
        + json.dumps({**base, "data_date": "2026-09-02"})
        + "\n"
    )

    with pytest.raises(bulk.SchemaMismatchError, match="mixed source dates"):
        fns_tax_regime.coalesce_tax_regime_records(source)


@pytest.mark.parametrize("kind", ("headcount", "msp"))
def test_postgresql_new_master_replays_headcount_and_msp_idempotently(
    next3_db, tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(bulk, "utc_now", lambda: NOW)
    suffix = str(uuid4().int % 10**8).zfill(8)
    first_inn, second_inn = f"77{suffix}", f"78{suffix}"
    if kind == "headcount":
        spec = fns_headcount._worker_spec()
        rows = [
            {
                "inn": first_inn,
                "data_date": "2025-12-31",
                "year": 2025,
                "employee_count": 10,
                "document_id": f"h1-{suffix}",
                "document_date": "2026-08-25",
            },
            {
                "inn": second_inn,
                "data_date": "2025-12-31",
                "year": 2025,
                "employee_count": 20,
                "document_id": f"h2-{suffix}",
                "document_date": "2026-08-25",
            },
        ]
        model = CompanyHeadcount
    else:
        spec = fns_msp._worker_spec()
        rows = [
            {
                "inn": first_inn,
                "data_date": "2026-09-10",
                "inclusion_date": "2020-01-01",
                "subject_type_code": "1",
                "category_code": "1",
                "is_new_code": "0",
                "social_enterprise_code": "0",
                "employee_count": 10,
                "document_id": f"m1-{suffix}",
            },
            {
                "inn": second_inn,
                "data_date": "2026-09-10",
                "inclusion_date": "2021-01-01",
                "subject_type_code": "1",
                "category_code": "2",
                "is_new_code": "0",
                "social_enterprise_code": "1",
                "employee_count": 20,
                "document_id": f"m2-{suffix}",
            },
        ]
        model = CompanyMspProfile
    release = _release(spec)
    snapshot = tmp_path / f"{kind}.jsonl"
    snapshot.write_text("".join(json.dumps(row) + "\n" for row in rows))
    checksum, _ = bulk._hash_file(snapshot)
    result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer=snapshot.as_uri(),
            checksum=checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={"release_identity": release.identity},
            ),
        ),
        counters=ExecutionCounters(records_seen=2, records_written=2),
    )
    with next3_db() as session:
        dataset = _source_and_dataset(session, spec.dataset_code)
        session.execute(sa.delete(model).where(model.dataset_id == dataset.id))
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == spec.source_id
            )
        )
        session.add(Company(inn=first_inn, name="first", entity_type="legal"))
        session.flush()
        published = bulk.publish_bulk_result(
            session, SimpleNamespace(schedule_metadata=release.as_metadata()), result, spec=spec
        )
        session.add(
            WorkerPublicationState(
                source_id=spec.source_id,
                active_pointer=snapshot.as_uri(),
                generation=1,
                last_fencing_token=1,
                validation_metadata={
                    "checksum": checksum,
                    "validation": published.staging_result.validation.metadata,
                },
            )
        )
        session.flush()
        assert session.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.dataset_id == dataset.id)
        ) == 1
        second = Company(inn=second_inn, name="second", entity_type="legal")
        session.add(second)
        session.flush()
        replay_claim = SimpleNamespace(
            schedule_metadata={
                **release.as_metadata(),
                "check_only": True,
                "replay_snapshot": True,
                "replay_pointer": snapshot.as_uri(),
                "replay_checksum": checksum,
            }
        )
        first_replay = bulk.publish_bulk_result(
            session, replay_claim, HandlerResult(), spec=spec
        )
        second_replay = bulk.publish_bulk_result(
            session, replay_claim, HandlerResult(), spec=spec
        )
        assert first_replay.counters.records_published == 1
        assert second_replay.counters.records_published == 0
        assert session.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.dataset_id == dataset.id)
        ) == 2


def test_postgresql_tax_regime_family_atomic_replay_and_applicability(
    next3_db, tmp_path, monkeypatch
):
    monkeypatch.setattr(bulk, "utc_now", lambda: NOW)
    monkeypatch.setattr(fns_tax_regime, "utc_now", lambda: NOW, raising=False)
    suffix = str(uuid4().int % 10**8).zfill(8)
    legal_first, legal_new = f"77{suffix}", f"78{suffix}"
    ip_new = f"66{suffix}11"
    specs = fns_tax_regime._member_specs()
    bundle = bulk.FnsReleaseBundle(
        {
            "legal": _release(specs["legal"], actual_until=NOW.date()),
            "ip": _release(
                specs["ip"], actual_until=NOW.date() + timedelta(days=5)
            ),
        }
    )
    member_rows = {
        "legal": [
            {
                "inn": legal_first,
                "entity_type": "legal",
                "dataset_code": "fns_snr",
                "data_date": "2025-12-31",
                "source_document_id": f"l1-{suffix}",
                "source_document_date": "2026-09-01",
                "regime_codes": ["usn"],
            },
            {
                "inn": legal_new,
                "entity_type": "legal",
                "dataset_code": "fns_snr",
                "data_date": "2025-12-31",
                "source_document_id": f"l2-{suffix}",
                "source_document_date": "2026-09-01",
                "regime_codes": ["eshn"],
            },
        ],
        "ip": [
            {
                "inn": ip_new,
                "entity_type": "individual_entrepreneur",
                "dataset_code": "fns_snrip",
                "data_date": "2025-12-31",
                "source_document_id": f"i1-{suffix}",
                "source_document_date": "2026-09-01",
                "regime_codes": ["psn"],
            }
        ],
    }
    members = {}
    for name, rows in member_rows.items():
        path = tmp_path / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        checksum, _ = bulk._hash_file(path)
        members[name] = {
            "dataset_code": specs[name].dataset_code,
            "staging_pointer": path.as_uri(),
            "normalized_sha256": checksum,
            "release": bundle.releases[name].as_metadata(),
            "coverage": {"records_seen": len(rows)},
        }
    descriptor = tmp_path / "bundle.json"
    descriptor.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "source_id": "fns_tax_regime",
                "release_identity": bundle.identity,
                "members": members,
                "immutable": True,
            },
            sort_keys=True,
        )
        + "\n"
    )
    descriptor_checksum, _ = bulk._hash_file(descriptor)
    result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer=descriptor.as_uri(),
            checksum=descriptor_checksum,
            validation=ValidationResult(
                accepted=True, metadata={"release_identity": bundle.identity}
            ),
        ),
        counters=ExecutionCounters(records_seen=3, records_written=3),
    )
    with next3_db() as session:
        family = _source_and_dataset(session, "fns_tax_regime")
        legal_dataset = _source_and_dataset(session, "fns_snr")
        ip_dataset = _source_and_dataset(session, "fns_snrip")
        session.execute(
            sa.delete(CompanyTaxRegimeSnapshot).where(
                CompanyTaxRegimeSnapshot.dataset_id.in_(
                    (legal_dataset.id, ip_dataset.id)
                )
            )
        )
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == "fns_tax_regime"
            )
        )
        session.add(Company(inn=legal_first, name="legal first", entity_type="legal"))
        session.flush()
        claim = SimpleNamespace(schedule_metadata=bundle.as_metadata())
        published = fns_tax_regime.publish_fns_tax_regime_worker_result(
            session, claim, result
        )
        session.add(
            WorkerPublicationState(
                source_id="fns_tax_regime",
                active_pointer=descriptor.as_uri(),
                generation=1,
                last_fencing_token=1,
                validation_metadata={
                    "checksum": descriptor_checksum,
                    "validation": published.staging_result.validation.metadata,
                },
            )
        )
        session.flush()
        assert family.record_count == 1
        session.add_all(
            [
                Company(inn=legal_new, name="legal new", entity_type="legal"),
                Company(
                    inn=ip_new,
                    name="ip new",
                    entity_type="individual_entrepreneur",
                ),
            ]
        )
        session.flush()
        replay_claim = SimpleNamespace(
            schedule_metadata={
                **bundle.as_metadata(),
                "check_only": True,
                "replay_snapshot": True,
                "replay_pointer": descriptor.as_uri(),
                "replay_checksum": descriptor_checksum,
            }
        )
        replay = fns_tax_regime.publish_fns_tax_regime_worker_result(
            session, replay_claim, HandlerResult()
        )
        again = fns_tax_regime.publish_fns_tax_regime_worker_result(
            session, replay_claim, HandlerResult()
        )
        assert replay.counters.records_published == 2
        assert again.counters.records_published == 0
        rows = session.execute(
            sa.select(
                Company.inn,
                DataSet.code,
                CompanyTaxRegimeSnapshot.entity_type,
            )
            .join(Company, Company.id == CompanyTaxRegimeSnapshot.company_id)
            .join(DataSet, DataSet.id == CompanyTaxRegimeSnapshot.dataset_id)
            .where(
                CompanyTaxRegimeSnapshot.dataset_id.in_(
                    (legal_dataset.id, ip_dataset.id)
                )
            )
        ).all()
        assert (legal_new, "fns_snr", "legal") in rows
        assert (ip_new, "fns_snrip", "individual_entrepreneur") in rows
        assert len(rows) == 3
        assert family.operational_status == "current"
        monkeypatch.setattr(
            bulk, "utc_now", lambda: NOW + timedelta(days=1)
        )
        # The publisher imports the shared clock on every call.
        fns_tax_regime.publish_fns_tax_regime_worker_result(
            session, replay_claim, HandlerResult()
        )
        assert family.operational_status == "stale"
        assert legal_dataset.operational_status == "stale"
        assert ip_dataset.operational_status == "current"
