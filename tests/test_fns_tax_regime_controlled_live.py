from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import partial
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_bulk_worker as bulk
from app.ingestion import fns_tax_regime
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerHandlerRegistration, WorkerJob, WorkerPublicationState
from app.services.fns_tax_regime_readiness import validate_same_release_identity_chain
from app.services.fns_tax_regime_publication_admission import canonical_sha256
from scripts import run_fns_tax_regime_controlled_live as operator
from workspace_app.monitoring_service import _event_type


NOW = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)


def _bundle():
    releases = {}
    for name, spec in fns_tax_regime._member_specs().items():
        structure = "20230425" if name == "legal" else "20241025"
        releases[name] = bulk.FnsRelease(
            source_page_url=spec.source_page_url,
            artifact_url=(
                f"https://file.nalog.ru/opendata/{spec.source_path}/"
                f"data-20260925-structure-{structure}.zip"
            ),
            xsd_url=(
                f"https://file.nalog.ru/opendata/{spec.source_path}/"
                f"structure-{structure}.xsd"
            ),
            source_data_date=date(2026, 9, 1),
            actual_until=date(2026, 10, 25),
            discovered_at=NOW,
            provenance="Данные на 01.09.2026",
            source_updated_at=date(2026, 9, 25),
        )
    return bulk.FnsReleaseBundle(releases)


def _old_bundle():
    fresh = _bundle()
    legal = fresh.releases["legal"]
    old_legal = replace(
        legal,
        artifact_url=legal.artifact_url.replace("data-20260925", "data-20260924"),
    )
    return bulk.FnsReleaseBundle({"legal": old_legal, "ip": fresh.releases["ip"]})


def _head(url):
    size = 62_028_770 if "snr/" in url else 293_908_849
    if url.endswith(".xsd"):
        size = 13_116 if "snr/" in url else 11_847
    return {
        "Content-Length": str(size),
        "ETag": '"test-etag"',
    }


def _accepted_metadata(bundle, checksum):
    return {
        "checksum": checksum,
        "validation": {
            "release_identity": bundle.identity,
            "source_data_date": bundle.source_data_date.isoformat(),
            "member_release_identities": {
                name: release.identity for name, release in bundle.releases.items()
            },
        },
    }


def _seed_accepted_descriptor(raw_root, bundle):
    members = {}
    for name, code in (("legal", "fns_snr"), ("ip", "fns_snrip")):
        release = bundle.releases[name]
        artifact_checksum = sha256(f"artifact:{name}".encode()).hexdigest()
        normalized_bytes = (json.dumps({"name": name}, sort_keys=True) + "\n").encode()
        normalized_checksum = sha256(normalized_bytes).hexdigest()
        normalized_path = (
            raw_root / fns_tax_regime.SOURCE_ID / artifact_checksum /
            f"normalized-{normalized_checksum}.jsonl"
        )
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.write_bytes(normalized_bytes)
        members[name] = {
            "dataset_code": code,
            "staging_pointer": normalized_path.resolve().as_uri(),
            "normalized_sha256": normalized_checksum,
            "artifact_sha256": artifact_checksum,
            "release": release.as_metadata(),
        }
    descriptor = {
        "manifest_version": 1,
        "source_id": fns_tax_regime.SOURCE_ID,
        "release_identity": bundle.identity,
        "members": members,
        "immutable": True,
    }
    descriptor_path = (
        raw_root / fns_tax_regime.SOURCE_ID / "bundles" / bundle.identity /
        "normalized-bundle.json"
    )
    descriptor_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(descriptor, ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor_path.write_bytes(payload)
    return descriptor_path.resolve().as_uri(), sha256(payload).hexdigest()


def _raw_state(raw_root):
    return {
        str(path.relative_to(raw_root)): sha256(path.read_bytes()).hexdigest()
        for path in raw_root.rglob("*") if path.is_file()
    } if raw_root.exists() else {}


def _datasets():
    source_urls = {
        "fns_tax_regime": fns_tax_regime.LEGAL_SOURCE_PAGE_URL,
        "fns_snr": fns_tax_regime.LEGAL_SOURCE_PAGE_URL,
        "fns_snrip": fns_tax_regime.IP_SOURCE_PAGE_URL,
    }
    return [
        SimpleNamespace(
            code=code,
            enabled=False,
            source_url=source_urls[code],
            operational_status="not_configured",
            last_success_at=None,
            last_data_date=None,
            official_actual_until=None,
        )
        for code in ("fns_tax_regime", "fns_snr", "fns_snrip")
    ]


def _current_datasets(bundle):
    datasets = _datasets()
    releases = bundle.releases
    identities = {
        "fns_tax_regime": bundle.identity,
        "fns_snr": releases["legal"].identity,
        "fns_snrip": releases["ip"].identity,
    }
    for dataset in datasets:
        dataset.enabled = True
        dataset.operational_status = "current"
        dataset.last_success_at = NOW
        dataset.last_data_date = bundle.source_data_date
        dataset.official_actual_until = bundle.actual_until
        dataset.coverage = {"release_identity": identities[dataset.code]}
        if dataset.code != "fns_tax_regime":
            dataset.coverage["source_data_date"] = bundle.source_data_date.isoformat()
        dataset.source_as_of = datetime.combine(bundle.source_data_date, datetime.min.time(), tzinfo=timezone.utc)
    datasets[0].coverage["family_bundle_identity"] = bundle.identity
    datasets[0].coverage["members"] = {
        "legal": {"release_identity": identities["fns_snr"], "source_data_date": bundle.source_data_date.isoformat()},
        "ip": {"release_identity": identities["fns_snrip"], "source_data_date": bundle.source_data_date.isoformat()},
    }
    return datasets


class FakeSession:
    def __init__(self, *, approval=True, state=None):
        self.approval = (
            SimpleNamespace(approved=True, enabled=True, live_mode=False)
            if approval
            else None
        )
        self.state = state

    def get(self, model, _identity):
        if model is WorkerHandlerRegistration:
            return self.approval
        if model is WorkerPublicationState:
            return self.state
        raise AssertionError(model)

    def scalars(self, _statement):
        return _datasets()

    def scalar(self, _statement):
        return 0


@pytest.fixture
def preflight_db(monkeypatch, tmp_path):
    """Real PostgreSQL rows, isolated by an outer rollback; no HOME access."""

    connection = engine.connect()
    transaction = connection.begin()
    assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    bundle = _bundle()
    raw_root = tmp_path / "raw"
    accepted_pointer, accepted_checksum = _seed_accepted_descriptor(raw_root, bundle)
    raw_before = _raw_state(raw_root)
    with factory() as session:
        source = session.scalar(sa.select(DataSource).where(DataSource.code == "c6_preflight_test"))
        if source is None:
            source = DataSource(
                code="c6_preflight_test",
                name="C6 preflight test",
                source_type="official",
                priority=10,
                enabled=True,
            )
            session.add(source)
            session.flush()
        for expected in _current_datasets(bundle):
            dataset = session.scalar(sa.select(DataSet).where(DataSet.code == expected.code))
            if dataset is None:
                dataset = DataSet(
                    source_id=source.id,
                    code=expected.code,
                    name=expected.code,
                    domain="taxes",
                    update_mode="bulk",
                    data_format="xml",
                )
                session.add(dataset)
            dataset.enabled = expected.enabled
            dataset.source_url = expected.source_url
            dataset.operational_status = expected.operational_status
            dataset.last_success_at = expected.last_success_at
            dataset.last_data_date = expected.last_data_date
            dataset.source_as_of = expected.source_as_of
            dataset.official_actual_until = expected.official_actual_until
            dataset.coverage = expected.coverage
            dataset.freshness_policy = "irregular"
            dataset.last_error = None
        approval = session.get(
            WorkerHandlerRegistration,
            (fns_tax_regime.SOURCE_ID, fns_tax_regime.HANDLER_VERSION),
        )
        if approval is None:
            approval = WorkerHandlerRegistration(
                source_id=fns_tax_regime.SOURCE_ID,
                handler_version=fns_tax_regime.HANDLER_VERSION,
                metadata_json={},
            )
            session.add(approval)
        approval.approved = True
        approval.enabled = True
        approval.live_mode = False
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        if publication is None:
            publication = WorkerPublicationState(source_id=fns_tax_regime.SOURCE_ID)
            session.add(publication)
        publication.generation = 1
        publication.active_pointer = accepted_pointer
        publication.validation_metadata = _accepted_metadata(bundle, accepted_checksum)
        session.commit()

    original_report = operator.build_preflight_report
    monkeypatch.setattr(operator, "SessionLocal", factory)
    monkeypatch.setattr(operator, "_discover_bundle", lambda: bundle)
    monkeypatch.setattr(
        operator,
        "build_preflight_report",
        partial(
            original_report,
            head=_head,
            disk_usage=lambda _path: SimpleNamespace(
                total=100 * 1024**3,
                used=10 * 1024**3,
                free=90 * 1024**3,
            ),
            config=SimpleNamespace(min_disk_free_percent=10),
            now=NOW,
        ),
    )
    try:
        yield SimpleNamespace(
            factory=factory, bundle=bundle, raw_root=raw_root,
            accepted_pointer=accepted_pointer, accepted_checksum=accepted_checksum,
            raw_before=raw_before,
        )
    finally:
        transaction.rollback()
        connection.close()


def _job_count(factory):
    with factory() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(WorkerJob))


def _dataset_state(factory):
    with factory() as session:
        return session.execute(
            sa.select(
                DataSet.code,
                DataSet.operational_status,
                DataSet.last_success_at,
                DataSet.last_data_date,
                DataSet.source_as_of,
                DataSet.last_error,
                DataSet.coverage,
            ).where(DataSet.code.in_(("fns_tax_regime", "fns_snr", "fns_snrip")))
            .order_by(DataSet.code)
        ).all()


def _publication_state(factory):
    with factory() as session:
        row = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        return None if row is None else (
            row.generation, row.active_pointer, deepcopy(row.validation_metadata)
        )


def _set_old_accepted_release(preflight_db, session):
    old = _old_bundle()
    pointer, checksum = _seed_accepted_descriptor(preflight_db.raw_root, old)
    publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
    publication.active_pointer = pointer
    publication.validation_metadata = _accepted_metadata(old, checksum)
    family = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_tax_regime"))
    family_coverage = deepcopy(family.coverage)
    family_coverage["release_identity"] = old.identity
    family_coverage["family_bundle_identity"] = old.identity
    for name, code in (("legal", "fns_snr"), ("ip", "fns_snrip")):
        family_coverage["members"][name]["release_identity"] = old.releases[name].identity
        child = session.scalar(sa.select(DataSet).where(DataSet.code == code))
        child_coverage = deepcopy(child.coverage)
        child_coverage["release_identity"] = old.releases[name].identity
        child.coverage = child_coverage
    family.coverage = family_coverage
    preflight_db.raw_before = _raw_state(preflight_db.raw_root)
    return old


def _alternate_member_identity(bundle, name):
    release = bundle.releases[name]
    alternate = replace(
        release,
        artifact_url=release.artifact_url.replace("data-20260925", "data-20260926"),
    )
    assert alternate.identity != release.identity
    spec = fns_tax_regime._member_specs()[name]
    bulk.validate_official_release_url(spec, alternate.artifact_url, artifact=True)
    return alternate.identity


IDENTITY_MUTATIONS = (
    ("legal_coordinated", "same_release_member_identity_mismatch"),
    ("ip_coordinated", "same_release_member_identity_mismatch"),
    ("both_coordinated", "same_release_member_identity_mismatch"),
    ("swapped", "same_release_member_identity_mismatch"),
    ("family_legal", "same_release_member_identity_mismatch"),
    ("family_ip", "same_release_member_identity_mismatch"),
    ("child_legal", "same_release_child_identity_mismatch"),
    ("child_ip", "same_release_child_identity_mismatch"),
    ("missing_member_legal", "same_release_member_identity_missing"),
    ("missing_member_ip", "same_release_member_identity_missing"),
    ("missing_child_legal", "same_release_child_identity_missing"),
    ("missing_child_ip", "same_release_child_identity_missing"),
    ("missing_map", "same_release_member_map_invalid"),
    ("null_map", "same_release_member_map_invalid"),
    ("list_map", "same_release_member_map_invalid"),
    ("string_map", "same_release_member_map_invalid"),
    ("partial_legal", "same_release_member_map_invalid"),
    ("partial_ip", "same_release_member_map_invalid"),
    ("nested_invalid", "same_release_member_map_invalid"),
    ("wrong_field_type", "same_release_member_identity_missing"),
    ("extra_member", "same_release_member_map_invalid"),
    ("member_date_legal", "same_release_member_date_mismatch"),
    ("member_date_ip", "same_release_member_date_mismatch"),
    ("child_coverage_date_legal", "same_release_child_date_mismatch"),
    ("child_coverage_date_ip", "same_release_child_date_mismatch"),
    ("child_date_legal", "same_release_child_date_mismatch"),
    ("child_date_ip", "same_release_child_date_mismatch"),
    ("family_date", "same_release_family_date_mismatch"),
    ("family_asof", "same_release_family_date_mismatch"),
    ("family_identity", "same_release_family_identity_mismatch"),
    ("missing_family_identity", "same_release_family_identity_missing"),
    ("family_bundle_identity", "same_release_family_bundle_identity_mismatch"),
    ("publication_metadata", "same_release_publication_invalid"),
    ("publication_identity", "same_release_publication_identity_mismatch"),
    ("publication_pointer", "same_release_publication_generation_invalid"),
    ("publication_generation", "same_release_publication_generation_invalid"),
    ("publication_checksum", "same_release_publication_generation_invalid"),
    ("publication_member_legal", "same_release_publication_member_identity_mismatch"),
    ("publication_member_ip", "same_release_publication_member_identity_mismatch"),
    ("publication_member_map", "same_release_publication_member_map_invalid"),
    ("publication_date", "same_release_publication_date_mismatch"),
    ("family_coverage_date", "same_release_family_date_mismatch"),
)


def _mutate_identity_graph(datasets, publication, bundle, mutation):
    family = datasets["fns_tax_regime"]
    family_coverage = deepcopy(family.coverage)
    children = {name: datasets[code] for name, code in (("legal", "fns_snr"), ("ip", "fns_snrip"))}
    child_coverage = {name: deepcopy(child.coverage) for name, child in children.items()}
    alternatives = {name: _alternate_member_identity(bundle, name) for name in children}

    if mutation in {"legal_coordinated", "ip_coordinated", "both_coordinated"}:
        names = ("legal", "ip") if mutation == "both_coordinated" else (mutation.split("_")[0],)
        for name in names:
            family_coverage["members"][name]["release_identity"] = alternatives[name]
            child_coverage[name]["release_identity"] = alternatives[name]
    elif mutation == "swapped":
        for name, other in (("legal", "ip"), ("ip", "legal")):
            family_coverage["members"][name]["release_identity"] = bundle.releases[other].identity
            child_coverage[name]["release_identity"] = bundle.releases[other].identity
    elif mutation.startswith("family_") and mutation in {"family_legal", "family_ip"}:
        name = mutation.split("_")[1]
        family_coverage["members"][name]["release_identity"] = alternatives[name]
    elif mutation.startswith("child_") and mutation in {"child_legal", "child_ip"}:
        name = mutation.split("_")[1]
        child_coverage[name]["release_identity"] = alternatives[name]
    elif mutation.startswith("missing_member_"):
        del family_coverage["members"][mutation.removeprefix("missing_member_")]["release_identity"]
    elif mutation.startswith("missing_child_"):
        del child_coverage[mutation.removeprefix("missing_child_")]["release_identity"]
    elif mutation == "missing_map":
        del family_coverage["members"]
    elif mutation in {"null_map", "list_map", "string_map"}:
        family_coverage["members"] = {"null_map": None, "list_map": [], "string_map": "bad"}[mutation]
    elif mutation.startswith("partial_"):
        family_coverage["members"].pop("ip" if mutation == "partial_legal" else "legal")
    elif mutation == "nested_invalid":
        family_coverage["members"]["legal"] = []
    elif mutation == "wrong_field_type":
        family_coverage["members"]["legal"]["release_identity"] = [bundle.releases["legal"].identity]
    elif mutation == "extra_member":
        family_coverage["members"]["other"] = {"release_identity": "extra"}
    elif mutation.startswith("member_date_"):
        family_coverage["members"][mutation.removeprefix("member_date_")]["source_data_date"] = "2026-08-01"
    elif mutation.startswith("child_coverage_date_"):
        child_coverage[mutation.removeprefix("child_coverage_date_")]["source_data_date"] = "2026-08-01"
    elif mutation.startswith("child_date_"):
        children[mutation.removeprefix("child_date_")].last_data_date = date(2026, 8, 1)
    elif mutation == "family_date":
        family.last_data_date = date(2026, 8, 1)
    elif mutation == "family_asof":
        family.source_as_of = datetime(2026, 8, 1, tzinfo=timezone.utc)
    elif mutation == "family_coverage_date":
        family_coverage["source_data_date"] = "2026-08-01"
    elif mutation == "family_identity":
        family_coverage["release_identity"] = "wrong-family"
    elif mutation == "missing_family_identity":
        del family_coverage["release_identity"]
    elif mutation == "family_bundle_identity":
        family_coverage["family_bundle_identity"] = "wrong-family"
    elif mutation == "publication_metadata":
        publication.validation_metadata = ["bad"]
    elif mutation == "publication_identity":
        publication.validation_metadata = {
            "validation": {"release_identity": "previous-release"}, "checksum": "a" * 64,
        }
    elif mutation == "publication_pointer":
        publication.active_pointer = None
    elif mutation == "publication_generation":
        publication.generation = 0
    elif mutation == "publication_checksum":
        publication.validation_metadata = {"validation": {"release_identity": bundle.identity}}
    elif mutation.startswith("publication_member_"):
        metadata = deepcopy(publication.validation_metadata)
        member_map = {name: release.identity for name, release in bundle.releases.items()}
        if mutation == "publication_member_map":
            member_map["other"] = "extra"
        else:
            member_map[mutation.removeprefix("publication_member_")] = alternatives[
                mutation.removeprefix("publication_member_")
            ]
        metadata["validation"]["member_release_identities"] = member_map
        publication.validation_metadata = metadata
    elif mutation == "publication_date":
        metadata = deepcopy(publication.validation_metadata)
        metadata["validation"]["source_data_date"] = "2026-08-01"
        publication.validation_metadata = metadata
    else:
        raise AssertionError(mutation)

    family.coverage = family_coverage
    for name, child in children.items():
        child.coverage = child_coverage[name]


@pytest.mark.parametrize(("mutation", "reason"), IDENTITY_MUTATIONS)
def test_pure_same_release_chain_rejects_identity_graph_mutation(mutation, reason):
    bundle = _bundle()
    datasets = {row.code: row for row in _current_datasets(bundle)}
    publication = SimpleNamespace(
        validation_metadata={"validation": {"release_identity": bundle.identity}, "checksum": "a" * 64},
        active_pointer="file:///accepted/c6-bundle.json", generation=1,
    )
    _mutate_identity_graph(datasets, publication, bundle, mutation)
    verdict = validate_same_release_identity_chain(bundle, datasets, publication)
    assert not verdict.valid
    assert reason in verdict.reasons
    if mutation == "legal_coordinated":
        assert verdict.member_verification["legal"]["mismatch_reasons"] == [
            "family_member_identity_mismatch", "child_dataset_identity_mismatch"
        ]
        assert verdict.member_verification["ip"]["valid"] is True


def test_pure_same_release_chain_accepts_only_fresh_authoritative_graph():
    bundle = _bundle()
    datasets = {row.code: row for row in _current_datasets(bundle)}
    publication = SimpleNamespace(
        validation_metadata={"validation": {"release_identity": bundle.identity}, "checksum": "a" * 64},
        active_pointer="file:///accepted/c6-bundle.json", generation=1,
    )
    verdict = validate_same_release_identity_chain(bundle, datasets, publication)
    assert verdict.valid
    assert verdict.reasons == ()
    assert verdict.expected_members == {name: bundle.releases[name].identity for name in ("legal", "ip")}
    assert verdict.member_verification["legal"]["valid"] is True
    assert verdict.member_verification["ip"]["valid"] is True


def test_pure_same_release_chain_rejects_incomplete_fresh_bundle_without_exception():
    bundle = _bundle()
    incomplete = bulk.FnsReleaseBundle({"legal": bundle.releases["legal"]})
    datasets = {row.code: row for row in _current_datasets(bundle)}
    publication = SimpleNamespace(
        validation_metadata={"validation": {"release_identity": incomplete.identity}, "checksum": "a" * 64},
        active_pointer="file:///accepted/c6-bundle.json", generation=1,
    )
    verdict = validate_same_release_identity_chain(incomplete, datasets, publication)
    assert not verdict.valid
    assert verdict.reasons == ("same_release_discovered_member_set_invalid",)


def test_pure_same_release_chain_requires_publication_state():
    bundle = _bundle()
    datasets = {row.code: row for row in _current_datasets(bundle)}
    verdict = validate_same_release_identity_chain(bundle, datasets, None)
    assert not verdict.valid
    assert "same_release_publication_invalid" in verdict.reasons


@pytest.mark.parametrize(("mutation", "reason"), [
    item for item in IDENTITY_MUTATIONS if item[0] not in {"publication_metadata", "publication_identity"}
])
def test_real_postgres_same_release_mutation_blocks_without_side_effects(preflight_db, mutation, reason):
    with preflight_db.factory() as session:
        datasets = {row.code: row for row in session.scalars(sa.select(DataSet).where(
            DataSet.code.in_(("fns_tax_regime", "fns_snr", "fns_snrip"))
        ))}
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        _mutate_identity_graph(datasets, publication, preflight_db.bundle, mutation)
        session.commit()
    before_datasets = _dataset_state(preflight_db.factory)
    before_publication = _publication_state(preflight_db.factory)
    before_jobs = _job_count(preflight_db.factory)
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "CORRUPT_PUBLICATION"
    assert report["publication_lifecycle"] == "CORRUPT"
    assert report["status"] == "BLOCKED"
    assert report["publication_proof_reasons"]
    assert _dataset_state(preflight_db.factory) == before_datasets
    assert _publication_state(preflight_db.factory) == before_publication
    assert _job_count(preflight_db.factory) == before_jobs
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before
    with pytest.raises(RuntimeError, match="controlled-live preflight is blocked"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert _dataset_state(preflight_db.factory) == before_datasets
    assert _publication_state(preflight_db.factory) == before_publication
    assert _job_count(preflight_db.factory) == before_jobs
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


@pytest.mark.parametrize("metadata", [
    ["bad"], {"validation": []}, {"validation": {}}, {},
])
def test_real_postgres_invalid_accepted_publication_fails_closed(preflight_db, metadata):
    with preflight_db.factory() as session:
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        publication.validation_metadata = metadata
        session.commit()
    before_datasets = _dataset_state(preflight_db.factory)
    before_publication = _publication_state(preflight_db.factory)
    before_jobs = _job_count(preflight_db.factory)
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "CORRUPT_PUBLICATION"
    assert report["status"] == "BLOCKED"
    assert report["publication_proof_reasons"]
    with pytest.raises(RuntimeError, match="controlled-live preflight is blocked"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert _dataset_state(preflight_db.factory) == before_datasets
    assert _publication_state(preflight_db.factory) == before_publication
    assert _job_count(preflight_db.factory) == before_jobs
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


def test_real_postgres_empty_existing_state_is_corrupt(preflight_db):
    with preflight_db.factory() as session:
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        publication.active_pointer = None
        publication.generation = 0
        publication.validation_metadata = {}
        session.commit()
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "CORRUPT_PUBLICATION"
    assert report["status"] == "BLOCKED"


@pytest.mark.parametrize("checksum", [
    "not-a-sha256", "", "a" * 63, "a" * 65, "A" * 64,
    "g" * 64, " a" * 64, "a" * 64 + " ",
])
@pytest.mark.parametrize("old_release", [False, True])
def test_real_postgres_checksum_mutation_never_admits(
    preflight_db, checksum, old_release
):
    with preflight_db.factory() as session:
        if old_release:
            _set_old_accepted_release(preflight_db, session)
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        metadata = deepcopy(publication.validation_metadata)
        metadata["checksum"] = checksum
        publication.validation_metadata = metadata
        session.commit()
    before = (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    )
    report, _ = operator._preflight(preflight_db.raw_root)
    assert report["publication_lifecycle"] == "CORRUPT"
    assert "accepted_checksum_invalid" in report["blockers"]
    assert report["status"] == "BLOCKED"
    with pytest.raises(RuntimeError, match="controlled-live preflight is blocked"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == before


@pytest.mark.parametrize("mutation", ["identity_removed", "identity_modified", "state_deleted"])
def test_real_postgres_lost_or_corrupt_identity_blocks_zero_jobs(preflight_db, mutation):
    with preflight_db.factory() as session:
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        if mutation == "state_deleted":
            session.delete(publication)
        else:
            metadata = deepcopy(publication.validation_metadata)
            if mutation == "identity_removed":
                del metadata["validation"]["release_identity"]
            else:
                metadata["validation"]["release_identity"] = "b" * 64
            publication.validation_metadata = metadata
        session.commit()
    before = (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    )
    report, _ = operator._preflight(preflight_db.raw_root)
    assert report["publication_lifecycle"] == "CORRUPT"
    assert report["release_mode"] == "CORRUPT_PUBLICATION"
    assert report["status"] == "BLOCKED"
    assert (
        "lost_accepted_publication_state" if mutation == "state_deleted" else
        "accepted_family_composition_mismatch" if mutation == "identity_modified" else
        "accepted_family_identity_invalid"
    ) in report["blockers"]
    with pytest.raises(RuntimeError, match="controlled-live preflight is blocked"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == before


@pytest.mark.parametrize("mutation", [
    "descriptor_missing", "descriptor_bytes", "invalid_json", "wrong_member",
    "wrong_release", "normalized_missing", "normalized_bytes",
])
def test_same_release_descriptor_tamper_blocks_zero_jobs(preflight_db, mutation):
    descriptor_path = preflight_db.raw_root / fns_tax_regime.SOURCE_ID / "bundles" / (
        preflight_db.bundle.identity
    ) / "normalized-bundle.json"
    original = descriptor_path.read_bytes()
    descriptor = json.loads(original)
    if mutation == "descriptor_missing":
        descriptor_path.unlink()
    elif mutation == "descriptor_bytes":
        descriptor_path.write_bytes(original + b" ")
    elif mutation == "invalid_json":
        descriptor_path.write_bytes(b"{")
    elif mutation == "wrong_member":
        descriptor["members"].pop("ip")
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    elif mutation == "wrong_release":
        descriptor["members"]["legal"]["release"]["release_identity"] = "b" * 64
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    else:
        member = descriptor["members"]["legal"]
        normalized_path = (
            preflight_db.raw_root / fns_tax_regime.SOURCE_ID /
            member["artifact_sha256"] / f"normalized-{member['normalized_sha256']}.jsonl"
        )
        if mutation == "normalized_missing":
            normalized_path.unlink()
        else:
            normalized_path.write_bytes(b"tampered\n")
    if mutation in {"invalid_json", "wrong_member", "wrong_release"}:
        with preflight_db.factory() as session:
            publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
            metadata = deepcopy(publication.validation_metadata)
            metadata["checksum"] = sha256(descriptor_path.read_bytes()).hexdigest()
            publication.validation_metadata = metadata
            session.commit()
    before = (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    )
    report, _ = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "SAME_RELEASE"
    assert report["status"] == "BLOCKED"
    assert report["accepted_descriptor_verdict"]["valid"] is False
    with pytest.raises(RuntimeError, match="controlled-live preflight is blocked"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == before


def test_real_postgres_substituted_replay_path_blocks_before_job(preflight_db):
    with preflight_db.factory() as session:
        publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
        publication.active_pointer = "file:///substituted/normalized-bundle.json"
        session.commit()
    before = (
        _dataset_state(preflight_db.factory),
        _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory),
    )
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "CORRUPT_PUBLICATION"
    assert report["status"] == "BLOCKED"
    assert "accepted_pointer_invalid" in report["blockers"]
    with pytest.raises(RuntimeError, match="accepted_pointer_invalid"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory),
        _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory),
    ) == before
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


@pytest.mark.parametrize(("race", "reason"), [
    ("member", "controlled_live_preflight_stale"),
    ("pointer", "controlled_live_preflight_stale"),
    ("pointer_path", "controlled_live_preflight_stale"),
    ("source_url", "controlled_live_preflight_stale"),
    ("handler", "controlled_live_preflight_stale"),
])
def test_confirmed_enqueue_rechecks_state_changed_after_preflight(
    preflight_db, monkeypatch, race, reason
):
    original_preflight = operator._preflight
    snapshots = []

    def racing_preflight(raw_root):
        report, bundle = original_preflight(raw_root)
        assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
        with preflight_db.factory() as session:
            if race == "member":
                family = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_tax_regime"))
                legal = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_snr"))
                family_coverage = deepcopy(family.coverage)
                legal_coverage = deepcopy(legal.coverage)
                alternate = _alternate_member_identity(bundle, "legal")
                family_coverage["members"]["legal"]["release_identity"] = alternate
                legal_coverage["release_identity"] = alternate
                family.coverage = family_coverage
                legal.coverage = legal_coverage
            elif race in {"pointer", "pointer_path"}:
                session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID).active_pointer = (
                    None if race == "pointer" else "file:///substituted/normalized-bundle.json"
                )
            elif race == "source_url":
                ip = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_snrip"))
                ip.source_url = "https://www.nalog.gov.ru/opendata/substituted/"
            else:
                approval = session.get(WorkerHandlerRegistration, (
                    fns_tax_regime.SOURCE_ID, fns_tax_regime.HANDLER_VERSION
                ))
                approval.approved = False
            session.commit()
        snapshots.append((_dataset_state(preflight_db.factory),
                          _publication_state(preflight_db.factory),
                          _job_count(preflight_db.factory)))
        return report, bundle

    monkeypatch.setattr(operator, "_preflight", racing_preflight)
    with pytest.raises(RuntimeError, match=reason):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert len(snapshots) == 1
    assert (_dataset_state(preflight_db.factory),
            _publication_state(preflight_db.factory),
            _job_count(preflight_db.factory)) == snapshots[0]
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


def test_new_to_same_release_race_cannot_bypass_member_chain(preflight_db, monkeypatch):
    with preflight_db.factory() as session:
        _set_old_accepted_release(preflight_db, session)
        session.commit()
    original_preflight = operator._preflight

    def racing_preflight(raw_root):
        report, bundle = original_preflight(raw_root)
        assert report["release_mode"] == "NEW_RELEASE"
        assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
        with preflight_db.factory() as session:
            publication = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
            publication.validation_metadata = _accepted_metadata(bundle, preflight_db.accepted_checksum)
            publication.active_pointer = preflight_db.accepted_pointer
            family = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_tax_regime"))
            legal = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_snr"))
            family_coverage = deepcopy(family.coverage)
            legal_coverage = deepcopy(legal.coverage)
            alternate = _alternate_member_identity(bundle, "legal")
            family_coverage["members"]["legal"]["release_identity"] = alternate
            legal_coverage["release_identity"] = alternate
            family.coverage = family_coverage
            legal.coverage = legal_coverage
            session.commit()
        return report, bundle

    monkeypatch.setattr(operator, "_preflight", racing_preflight)
    before_jobs = _job_count(preflight_db.factory)
    with pytest.raises(RuntimeError, match="controlled_live_preflight_stale"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert _job_count(preflight_db.factory) == before_jobs
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


@pytest.mark.parametrize("attempt", range(5))
@pytest.mark.parametrize("race", ["generation", "checksum", "state_deleted"])
def test_repeated_same_release_admission_races_leave_zero_jobs(
    preflight_db, monkeypatch, race, attempt
):
    original_preflight = operator._preflight
    snapshots = []

    def racing_preflight(raw_root):
        report, bundle = original_preflight(raw_root)
        assert report["release_mode"] == "SAME_RELEASE"
        assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
        with preflight_db.factory() as session:
            state = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
            if race == "generation":
                state.generation = 2
            elif race == "checksum":
                metadata = deepcopy(state.validation_metadata)
                metadata["checksum"] = "b" * 64
                state.validation_metadata = metadata
            else:
                session.delete(state)
            session.commit()
        snapshots.append((
            _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
            _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
        ))
        return report, bundle

    monkeypatch.setattr(operator, "_preflight", racing_preflight)
    with pytest.raises(RuntimeError, match="controlled_live_preflight_stale"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == snapshots[0]


@pytest.mark.parametrize("attempt", range(5))
def test_repeated_initial_to_accepted_race_is_stale(preflight_db, monkeypatch, attempt):
    with preflight_db.factory() as session:
        session.delete(session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID))
        for row in session.scalars(sa.select(DataSet).where(DataSet.code.in_(
            ("fns_tax_regime", "fns_snr", "fns_snrip")
        ))):
            row.coverage = {}
            row.last_success_at = None
            row.last_data_date = None
            row.source_as_of = None
            row.published_at = None
            row.record_count = 0
        session.commit()
    original_preflight = operator._preflight
    snapshots = []

    def racing_preflight(raw_root):
        report, bundle = original_preflight(raw_root)
        assert report["release_mode"] == "INITIAL_RELEASE"
        assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
        with preflight_db.factory() as session:
            for expected in _current_datasets(bundle):
                row = session.scalar(sa.select(DataSet).where(DataSet.code == expected.code))
                row.coverage = expected.coverage
                row.last_success_at = expected.last_success_at
                row.last_data_date = expected.last_data_date
                row.source_as_of = expected.source_as_of
            session.add(WorkerPublicationState(
                source_id=fns_tax_regime.SOURCE_ID,
                generation=1,
                active_pointer=preflight_db.accepted_pointer,
                validation_metadata=_accepted_metadata(bundle, preflight_db.accepted_checksum),
            ))
            session.commit()
        snapshots.append((
            _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
            _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
        ))
        return report, bundle

    monkeypatch.setattr(operator, "_preflight", racing_preflight)
    with pytest.raises(RuntimeError, match="controlled_live_preflight_stale"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == snapshots[0]


@pytest.mark.parametrize("attempt", range(5))
@pytest.mark.parametrize("race", ["generation", "checksum"])
def test_repeated_new_release_old_proof_race_is_stale(
    preflight_db, monkeypatch, race, attempt
):
    with preflight_db.factory() as session:
        _set_old_accepted_release(preflight_db, session)
        session.commit()
    original_preflight = operator._preflight
    snapshots = []

    def racing_preflight(raw_root):
        report, bundle = original_preflight(raw_root)
        assert report["release_mode"] == "NEW_RELEASE"
        assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
        with preflight_db.factory() as session:
            state = session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID)
            if race == "generation":
                state.generation = 2
            else:
                metadata = deepcopy(state.validation_metadata)
                metadata["checksum"] = "b" * 64
                state.validation_metadata = metadata
            session.commit()
        snapshots.append((
            _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
            _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
        ))
        return report, bundle

    monkeypatch.setattr(operator, "_preflight", racing_preflight)
    with pytest.raises(RuntimeError, match="controlled_live_preflight_stale"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == snapshots[0]


def test_disk_safety_is_rechecked_after_ready_preflight(preflight_db, monkeypatch):
    remaining = {"free": 90 * 1024**3}
    original_report = operator.build_preflight_report.func
    monkeypatch.setattr(operator, "build_preflight_report", partial(
        original_report,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3, used=100 * 1024**3 - remaining["free"],
            free=remaining["free"],
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
        now=NOW,
    ))
    original_preflight = operator._preflight

    def racing_preflight(raw_root):
        report, bundle = original_preflight(raw_root)
        assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
        remaining["free"] = 1 * 1024**3
        return report, bundle

    monkeypatch.setattr(operator, "_preflight", racing_preflight)
    before = (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    )
    with pytest.raises(RuntimeError, match="controlled_live_preflight_stale"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert (
        _dataset_state(preflight_db.factory), _publication_state(preflight_db.factory),
        _job_count(preflight_db.factory), _raw_state(preflight_db.raw_root),
    ) == before


def test_preflight_is_read_only_scoped_and_reports_new_bundle(tmp_path):
    report = operator.build_preflight_report(
        FakeSession(),
        bundle=_bundle(),
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=10 * 1024**3,
            free=90 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
    )

    assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
    assert report["release_mode"] == "INITIAL_RELEASE"
    assert report["family_readiness_state"] == "NOT_CHECKED"
    assert report["discovered_release_identity"] == _bundle().identity
    assert report["source_id"] == "fns_tax_regime"
    assert report["member_dataset_codes"] == ["fns_snr", "fns_snrip"]
    assert report["new_release_exists"] is True
    assert report["production_mutation"] is False
    assert set(report["resources"]["artifacts"]) == {"legal", "ip"}
    assert report["resources"]["estimated_peak_staging_and_raw_bytes"] == (
        report["resources"]["download_bytes"] * 4
    )


def test_preflight_fails_closed_for_handler_or_disk_safety(tmp_path):
    report = operator.build_preflight_report(
        FakeSession(approval=False),
        bundle=_bundle(),
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=10 * 1024**3,
            used=9 * 1024**3,
            free=1 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=1),
    )

    assert report["status"] == "BLOCKED"
    assert "durable_handler_approval_missing" in report["blockers"]
    assert "production_disk_free_percent_was_lowered" in report["blockers"]
    assert "fixed_free_space_floor_not_met_after_estimated_peak" in report["blockers"]


def test_preflight_fails_closed_for_substituted_dataset_source_url(
    tmp_path,
    monkeypatch,
):
    datasets = _datasets()
    datasets[-1].source_url = "https://www.nalog.gov.ru/opendata/substituted/"
    session = FakeSession()
    monkeypatch.setattr(session, "scalars", lambda _statement: datasets)

    report = operator.build_preflight_report(
        session,
        bundle=_bundle(),
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=10 * 1024**3,
            free=90 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
    )

    assert report["status"] == "BLOCKED"
    assert report["source_url_mismatches"] == ["fns_snrip"]
    assert "dataset_source_url_mismatch" in report["blockers"]


def test_preflight_recognizes_same_accepted_bundle(tmp_path, monkeypatch):
    bundle = _bundle()
    pointer, checksum = _seed_accepted_descriptor(tmp_path, bundle)
    state = SimpleNamespace(
        source_id=fns_tax_regime.SOURCE_ID,
        validation_metadata=_accepted_metadata(bundle, checksum),
        active_pointer=pointer,
        generation=1,
        last_fencing_token=0,
    )
    session = FakeSession(state=state)
    monkeypatch.setattr(session, "scalars", lambda _statement: _current_datasets(bundle))
    report = operator.build_preflight_report(
        session,
        bundle=bundle,
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=10 * 1024**3,
            free=90 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
    )

    assert report["new_release_exists"] is False
    assert report["release_mode"] == "SAME_RELEASE"
    assert report["family_readiness_state"] == "FOUND"
    assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
    assert report["publication_lifecycle"] == "ACCEPTED_VALID"
    assert report["accepted_descriptor_verdict"]["valid"] is True
    assert canonical_sha256(report["admission_fingerprint"])
    assert report["current_publication_identity"] == bundle.identity


def test_same_release_current_family_enqueues_one_check_only_job(preflight_db):
    before = _job_count(preflight_db.factory)
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "SAME_RELEASE"
    assert report["family_readiness_state"] == "FOUND"
    assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
    assert _job_count(preflight_db.factory) == before

    result = operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert result["check_only"] is True
    assert result["production_scheduler_enabled"] is False
    assert _job_count(preflight_db.factory) == before + 1
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


@pytest.mark.parametrize(
    ("code", "status", "error", "expected_state"),
    [
        ("fns_snr", "unavailable", None, "SOURCE_UNAVAILABLE"),
        ("fns_snr", "stale", None, "STALE_DATA"),
        ("fns_snr", "error", "XSD schema mismatch", "PARSING_ERROR"),
        ("fns_snrip", "unavailable", None, "SOURCE_UNAVAILABLE"),
        ("fns_snrip", "stale", None, "STALE_DATA"),
        ("fns_snrip", "error", "XML parse failure", "PARSING_ERROR"),
        ("fns_tax_regime", "unavailable", None, "SOURCE_UNAVAILABLE"),
        ("fns_tax_regime", "stale", None, "STALE_DATA"),
        ("fns_tax_regime", "error", "XSD schema mismatch", "PARSING_ERROR"),
        ("fns_tax_regime", "unverified", None, "NOT_CHECKED"),
    ],
)
def test_same_release_family_failure_blocks_confirmed_enqueue_without_job(
    preflight_db, code, status, error, expected_state
):
    with preflight_db.factory() as session:
        dataset = session.scalar(sa.select(DataSet).where(DataSet.code == code))
        if status == "unverified":
            dataset.last_success_at = None
        else:
            dataset.operational_status = status
        dataset.last_error = error
        session.commit()

    before_datasets = _dataset_state(preflight_db.factory)
    before_jobs = _job_count(preflight_db.factory)
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "SAME_RELEASE"
    assert report["current_publication_identity"] == preflight_db.bundle.identity
    assert report["discovered_release_identity"] == preflight_db.bundle.identity
    assert report["family_readiness_state"] == expected_state
    assert report["family_readiness_reason"].startswith(f"{code}:")
    assert report["status"] == "BLOCKED"
    assert "same_release_family_readiness_not_current" in report["blockers"]
    assert _dataset_state(preflight_db.factory) == before_datasets
    assert _job_count(preflight_db.factory) == before_jobs
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before

    with pytest.raises(RuntimeError, match="same_release_family_readiness_not_current"):
        operator._enqueue(preflight_db.raw_root, confirm=operator.CONFIRM_TOKEN)
    assert _dataset_state(preflight_db.factory) == before_datasets
    assert _job_count(preflight_db.factory) == before_jobs
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


def test_same_release_persisted_identity_mismatch_blocks(preflight_db):
    with preflight_db.factory() as session:
        family = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_tax_regime"))
        family.coverage = {**family.coverage, "release_identity": "different-release"}
        session.commit()
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["family_readiness_state"] == "FOUND"
    assert report["status"] == "BLOCKED"
    assert "accepted_family_dataset_identity_mismatch" in report["blockers"]
    assert _job_count(preflight_db.factory) == 0


def test_initial_release_not_blocked_by_unpublished_family(preflight_db):
    with preflight_db.factory() as session:
        session.delete(session.get(WorkerPublicationState, fns_tax_regime.SOURCE_ID))
        for dataset in session.scalars(
            sa.select(DataSet).where(DataSet.code.in_(("fns_tax_regime", "fns_snr", "fns_snrip")))
        ):
            dataset.operational_status = "not_configured"
            dataset.last_success_at = None
            dataset.last_data_date = None
            dataset.source_as_of = None
            dataset.published_at = None
            dataset.coverage = {}
            dataset.record_count = 0
        session.commit()
    before = _dataset_state(preflight_db.factory)
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "INITIAL_RELEASE"
    assert report["publication_lifecycle"] == "NO_PUBLICATION"
    assert report["current_publication_identity"] is None
    assert report["family_readiness_state"] == "NOT_CHECKED"
    assert report["new_release_exists"] is True
    assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
    assert _dataset_state(preflight_db.factory) == before
    assert _job_count(preflight_db.factory) == 0
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


@pytest.mark.parametrize(
    ("old_status", "expected_state"),
    [("stale", "STALE_DATA"), ("unavailable", "SOURCE_UNAVAILABLE")],
)
def test_new_release_old_family_is_diagnostic_only(
    preflight_db, old_status, expected_state
):
    with preflight_db.factory() as session:
        old = _set_old_accepted_release(preflight_db, session)
        ip = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_snrip"))
        ip.operational_status = old_status
        session.commit()
    before = _dataset_state(preflight_db.factory)
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == "NEW_RELEASE"
    assert report["publication_lifecycle"] == "ACCEPTED_VALID"
    assert report["current_publication_identity"] == old.identity
    assert report["discovered_release_identity"] == preflight_db.bundle.identity
    assert report["family_readiness_state"] == expected_state
    assert report["family_readiness_reason"] == f"fns_snrip:dataset_{old_status}"
    assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
    assert _dataset_state(preflight_db.factory) == before
    assert _job_count(preflight_db.factory) == 0
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


@pytest.mark.parametrize(
    ("violation", "blocker"),
    [
        ("source_url", "dataset_source_url_mismatch"),
        ("missing_dataset", "family_or_member_dataset_registration_missing"),
        ("handler", "durable_handler_approval_inactive_or_live_mode"),
        ("disk", "fixed_free_space_floor_not_met_after_estimated_peak"),
        ("missing_member", "discovered_bundle_invalid"),
        ("bad_path", "discovered_bundle_invalid"),
    ],
)
def test_new_release_keeps_independent_safety_gates(
    preflight_db, monkeypatch, violation, blocker
):
    with preflight_db.factory() as session:
        _set_old_accepted_release(preflight_db, session)
        if violation == "source_url":
            ip = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_snrip"))
            ip.source_url = "https://www.nalog.gov.ru/opendata/substituted/"
        elif violation == "missing_dataset":
            ip = session.scalar(sa.select(DataSet).where(DataSet.code == "fns_snrip"))
            session.delete(ip)
        elif violation == "handler":
            approval = session.get(
                WorkerHandlerRegistration,
                (fns_tax_regime.SOURCE_ID, fns_tax_regime.HANDLER_VERSION),
            )
            approval.approved = False
        session.commit()

    if violation == "disk":
        original = operator.build_preflight_report.func
        monkeypatch.setattr(
            operator,
            "build_preflight_report",
            partial(
                original,
                head=_head,
                disk_usage=lambda _path: SimpleNamespace(
                    total=10 * 1024**3,
                    used=9 * 1024**3,
                    free=1 * 1024**3,
                ),
                config=SimpleNamespace(min_disk_free_percent=10),
                now=NOW,
            ),
        )
    elif violation == "missing_member":
        monkeypatch.setattr(
            operator,
            "_discover_bundle",
            lambda: bulk.FnsReleaseBundle({"legal": preflight_db.bundle.releases["legal"]}),
        )
    elif violation == "bad_path":
        legal = preflight_db.bundle.releases["legal"]
        invalid = bulk.FnsRelease(
            **{
                **legal.__dict__,
                "artifact_url": legal.artifact_url.replace(
                    "/7707329152-snr/", "/7707329152-snr/../7707329152-snrip/"
                ),
            }
        )
        monkeypatch.setattr(
            operator,
            "_discover_bundle",
            lambda: bulk.FnsReleaseBundle(
                {"legal": invalid, "ip": preflight_db.bundle.releases["ip"]}
            ),
        )
    report, _bundle = operator._preflight(preflight_db.raw_root)
    assert report["release_mode"] == (
        "CORRUPT_PUBLICATION" if violation == "missing_dataset" else "NEW_RELEASE"
    )
    assert report["status"] == "BLOCKED"
    assert blocker in report["blockers"]
    assert _job_count(preflight_db.factory) == 0
    assert _raw_state(preflight_db.raw_root) == preflight_db.raw_before


def test_new_release_invalid_path_blocks_before_head(tmp_path, monkeypatch):
    bundle = _bundle()
    old = _old_bundle()
    legal = bundle.releases["legal"]
    sibling_path = fns_tax_regime._member_specs()["ip"].source_path
    invalid = bulk.FnsRelease(
        **{
            **legal.__dict__,
            "artifact_url": legal.artifact_url.replace(
                "/7707329152-snr/", f"/7707329152-snr/../{sibling_path}/"
            ),
        }
    )
    malformed = bulk.FnsReleaseBundle({"legal": invalid, "ip": bundle.releases["ip"]})
    session = FakeSession(
        state=SimpleNamespace(
            source_id=fns_tax_regime.SOURCE_ID,
            validation_metadata=_accepted_metadata(old, "a" * 64),
            active_pointer=(tmp_path / "raw" / fns_tax_regime.SOURCE_ID / "bundles" /
                            old.identity / "normalized-bundle.json").resolve().as_uri(),
            generation=1,
            last_fencing_token=0,
        )
    )
    monkeypatch.setattr(session, "scalars", lambda _statement: _current_datasets(old))
    report = operator.build_preflight_report(
        session,
        bundle=malformed,
        raw_root=tmp_path / "raw",
        head=lambda _url: pytest.fail("HEAD called for invalid bundle"),
        now=NOW,
    )
    assert report["release_mode"] == "NEW_RELEASE"
    assert report["status"] == "BLOCKED"
    assert "discovered_bundle_invalid" in report["blockers"]
    assert report["resources"] is None
    assert not (tmp_path / "raw").exists()


def test_controlled_enqueue_requires_exact_confirmation_token(tmp_path):
    try:
        operator._enqueue(tmp_path, confirm="wrong")
    except RuntimeError as error:
        assert "confirmation token differs" in str(error)
    else:
        raise AssertionError("controlled enqueue accepted a substituted token")


def test_passport_403_fails_closed_before_session_or_enqueue(tmp_path, monkeypatch):
    def unavailable():
        raise bulk.WorkerNetworkError("FNS request failed: HTTP Error 403")

    monkeypatch.setattr(operator, "_discover_bundle", unavailable)
    monkeypatch.setattr(
        operator, "SessionLocal", lambda: pytest.fail("preflight opened a session")
    )
    monkeypatch.setattr(
        operator,
        "enqueue_bulk_release_bundle",
        lambda *_args, **_kwargs: pytest.fail("unexpected enqueue"),
    )
    with pytest.raises(bulk.WorkerNetworkError, match="403"):
        operator._enqueue(tmp_path, confirm=operator.CONFIRM_TOKEN)


def test_controlled_enqueue_creates_one_scoped_job_without_running_it(
    tmp_path,
    monkeypatch,
):
    bundle = _bundle()
    session = FakeSession()
    session.committed = False
    session.scalar_calls = 0
    def scalar(_statement):
        session.scalar_calls += 1
        if session.scalar_calls == 1:
            return _datasets()[0]
        return None if session.scalar_calls == 2 else session.approval
    session.scalar = scalar
    session.__enter__ = lambda: session
    session.__exit__ = lambda *_args: None
    session.commit = lambda: setattr(session, "committed", True)
    captured = {}

    class SessionContext:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return None

    def fake_enqueue(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            created=True,
            job=SimpleNamespace(
                id="job-1",
                status="queued",
                job_type="fns_tax_regime_release",
            ),
        )

    fake_report = {
        "status": "READY_FOR_CONTROLLED_LIVE",
        "release_mode": "INITIAL_RELEASE",
        "admission_fingerprint": "test-fingerprint",
    }
    monkeypatch.setattr(operator, "_preflight", lambda _raw_root: (fake_report, bundle))
    monkeypatch.setattr(operator, "build_preflight_report", lambda *_args, **_kwargs: fake_report)
    monkeypatch.setattr(operator, "SessionLocal", SessionContext)
    monkeypatch.setattr(operator, "enqueue_bulk_release_bundle", fake_enqueue)

    result = operator._enqueue(tmp_path, confirm=operator.CONFIRM_TOKEN)

    assert session.committed is True
    assert result["status"] == "ENQUEUED"
    assert result["production_scheduler_enabled"] is False
    assert result["check_only"] is False
    assert "--allow-source fns_tax_regime" in result["worker_command"]
    assert "--work-budget 1" in result["worker_command"]
    assert captured["bundle"] is bundle
    assert captured["extra_schedule_metadata"] == {
        "controlled_live": True,
        "controlled_live_scope": "fns_tax_regime",
        "operator": "c6-home-controlled-live-v1",
    }


def test_tax_regime_semantic_coordinate_uses_generic_monitoring_event():
    assert _event_type("tax", "regime") == "GENERIC_FACT_CHANGED"


@pytest.mark.parametrize("member", ("legal", "ip"))
@pytest.mark.parametrize("path_kind", (
    "literal_traversal", "encoded_traversal", "nested_traversal",
    "encoded_separator", "malformed_percent", "wrong_sibling", "backslash",
))
def test_discovery_rejects_ambiguous_official_links(member, path_kind):
    spec = fns_tax_regime._member_specs()[member]
    sibling = fns_tax_regime._member_specs()["ip" if member == "legal" else "legal"]
    base = f"/opendata/{spec.source_path}/"
    name = "data-20260925-structure-20230425.zip"
    path = {
        "literal_traversal": base + "../" + sibling.source_path + "/" + name,
        "encoded_traversal": base + "%2e%2e/" + sibling.source_path + "/" + name,
        "nested_traversal": base + "%252e%252e/" + sibling.source_path + "/" + name,
        "encoded_separator": base + "%2F../" + name,
        "malformed_percent": base + "data-20260925-structure-20230425%GG.zip",
        "wrong_sibling": f"/opendata/{sibling.source_path}/{name}",
        "backslash": base + "..\\" + sibling.source_path + "/" + name,
    }[path_kind]
    html = f'''
      <td property="dc:identifier">{spec.source_path}</td>
      <a href="https://file.nalog.ru{path}">ZIP</a>
      <a href="https://file.nalog.ru{base}structure-20230425.xsd">XSD</a>
      <td property="dc:modified" content="25.09.2026">25.09.2026</td>
      <td property="dc:provenance">Данные на 01.09.2026</td>
    '''.encode()
    with pytest.raises((bulk.InvalidDataError, bulk.SchemaMismatchError)):
        bulk.discover_fns_release(spec, now=NOW, fetch=lambda _url: (html, {}))


@pytest.mark.parametrize("member", ("legal", "ip"))
@pytest.mark.parametrize("path_kind", (
    "literal_traversal", "encoded_traversal", "nested_traversal",
    "wrong_sibling", "xsd_sibling", "encoded_separator", "malformed_percent",
))
def test_staging_rejects_ambiguous_links_before_download(
    member, path_kind, tmp_path, monkeypatch
):
    spec = fns_tax_regime._member_specs()[member]
    sibling = fns_tax_regime._member_specs()["ip" if member == "legal" else "legal"]
    release = _bundle().releases[member]
    base = f"https://file.nalog.ru/opendata/{spec.source_path}/"
    name = release.artifact_url.rsplit("/", 1)[-1]
    artifact_url = {
        "literal_traversal": base + "../" + sibling.source_path + "/" + name,
        "encoded_traversal": base + "%2e%2e/" + sibling.source_path + "/" + name,
        "nested_traversal": base + "%252e%252e/" + sibling.source_path + "/" + name,
        "wrong_sibling": f"https://file.nalog.ru/opendata/{sibling.source_path}/{name}",
        "encoded_separator": base + "%2F../" + name,
        "malformed_percent": base + name.replace(".zip", "%GG.zip"),
    }.get(path_kind, release.artifact_url)
    xsd_url = (
        f"https://file.nalog.ru/opendata/{sibling.source_path}/"
        + release.xsd_url.rsplit("/", 1)[-1]
        if path_kind == "xsd_sibling" else release.xsd_url
    )
    calls = []
    monkeypatch.setattr(
        bulk, "_download_temp", lambda *args: calls.append(args)
    )
    invalid = bulk.FnsRelease(
        **{**release.__dict__, "artifact_url": artifact_url, "xsd_url": xsd_url}
    )
    raw_root = tmp_path / "raw"
    with pytest.raises(bulk.InvalidDataError):
        bulk.stage_release(spec, invalid, raw_root=raw_root)
    assert calls == []
    assert not raw_root.exists()


def test_current_legal_and_ip_urls_pass_canonical_validation():
    for member, release in _bundle().releases.items():
        spec = fns_tax_regime._member_specs()[member]
        assert bulk.validate_official_release_url(
            spec, release.artifact_url, artifact=True
        )[1] == bulk.validate_official_release_url(
            spec, release.xsd_url, artifact=False
        )[1]


@pytest.mark.parametrize("bad", ("%", "%0", "%GG", "%2Z"))
def test_canonical_url_rejects_malformed_percent_at_each_decode_layer(bad):
    spec = fns_tax_regime._member_specs()["legal"]
    url = (
        "https://file.nalog.ru/opendata/7707329152-snr/"
        f"data-20260925-structure-20230425{bad}.zip"
    )
    with pytest.raises(bulk.InvalidDataError):
        bulk.validate_official_release_url(spec, url, artifact=True)


@pytest.mark.parametrize("url", (
    "https://user@file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip",
    "https://file.nalog.ru:443/opendata/7707329152-snr/data-20260925-structure-20230425.zip",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip?download=1",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip?",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip#fragment",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip#",
))
def test_canonical_url_rejects_authority_query_and_fragment(url):
    with pytest.raises(bulk.InvalidDataError):
        bulk.validate_official_release_url(
            fns_tax_regime._member_specs()["legal"], url, artifact=True
        )


@pytest.mark.parametrize("control", ("\r", "\n", "\t", "\x00", "\x7f"))
def test_canonical_url_rejects_raw_controls_before_urlparse(control):
    url = (
        "https://file.nalog.ru/opendata/7707329152-snr/"
        f"data-20260925-structure-20230425{control}.zip"
    )
    with pytest.raises(bulk.InvalidDataError):
        bulk.validate_official_release_url(
            fns_tax_regime._member_specs()["legal"], url, artifact=True
        )
