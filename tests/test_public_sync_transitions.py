from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.models.publication import PublicPublicationRequest
from app.services import publication_service as service
from public_app.contracts import (
    HASH_ALGORITHM_VERSION,
    PROJECTION_VERSION,
    CanonicalManifest,
    ManifestEntity,
    PublicProjection,
)
from scripts import accept_public_launch as launch
from scripts import run_public_sync as runner
from scripts.public_release_common import (
    canonical_json,
    load_bundle,
    payload_sha256,
    raw_payload_sha256,
    semantic_projection_sha256,
)
from tests.public_test_support import forty_projections, projection


NOW = datetime(2026, 9, 29, 8, tzinfo=UTC)
SHA = "d" * 40
PARENT_40 = "public-v1-20260927T040214Z-32334077-8f44d69a"
ALAN_INN = "0100000614"
RETAINED_CONTROL_INN = "0100000639"


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _ReadOnlyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement):
        assert statement == "SET TRANSACTION READ ONLY"

    def cursor(self):
        return _Cursor()


class _BuildSession:
    def __init__(self, states, runs=None):
        self.states = states
        self.runs = {run.id: run for run in (runs or [])}

    def scalars(self, _query):
        return list(self.states)

    def get(self, _model, identifier):
        return self.runs.get(identifier)

    def add(self, _item):
        pytest.fail("test baseline unexpectedly inserted a publication state")


class _BoundarySession(_BuildSession):
    def __init__(self, states, runs, requests):
        super().__init__(states, runs)
        self.requests = list(requests)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def _active_requests(self):
        return [
            row
            for row in self.requests
            if row.status in service.ACTIVE_REQUEST_STATUSES
        ]

    def scalar(self, query):
        statement = str(query)
        if "public_publication_requests" not in statement:
            return None
        predicate = statement.split("WHERE", 1)[-1]
        if "recovered_from_request_id =" in predicate:
            failed_id = next(
                (
                    row.id
                    for row in self.requests
                    if row.status == "FAILED"
                ),
                None,
            )
            return next(
                (
                    row
                    for row in reversed(self.requests)
                    if row.recovered_from_request_id == failed_id
                ),
                None,
            )
        if "status IN" in predicate:
            return next(iter(self._active_requests()), None)
        return next(
            (
                row
                for row in reversed(self.requests)
                if row.status == "FAILED"
            ),
            None,
        )

    def scalars(self, query):
        statement = str(query)
        if "public_publication_requests" in statement:
            return list(self._active_requests())
        return list(self.states)

    def add(self, item):
        if isinstance(item, PublicPublicationRequest):
            if item.id is None:
                item.id = uuid4()
            self.requests.append(item)
            return
        pytest.fail("test unexpectedly inserted a publication state")

    def flush(self):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def get(self, model, identifier):
        if model is PublicPublicationRequest:
            return next(
                (row for row in self.requests if row.id == identifier),
                None,
            )
        return super().get(model, identifier)


class _Request(SimpleNamespace):
    def __init__(self):
        super().__init__(
            created_main_sha=SHA,
            status="BUILDING",
            candidate_release_id=None,
            previous_release_id=None,
            projection_generation=None,
            projection_hash=None,
            changed_company_count=0,
            changed_company_ids=[],
            changed_company_inns=[],
            change_summary=[],
            ready_at=None,
            last_error=None,
            completed_at=None,
            updated_at=NOW,
        )


def _manifest(inns: list[str]) -> CanonicalManifest:
    return CanonicalManifest(
        schema_version="canonical-public-cohort-v1",
        manifest_version=2,
        release_name="transition-test",
        created_at=NOW,
        source_main_sha="b" * 40,
        source_database="nextcompany_operational",
        production_eligibility="VERIFIED_OPERATIONAL",
        selection_policy={"version": "transition-test", "risk_outcome_used": False},
        entities=tuple(
            ManifestEntity(
                inn=inn,
                entity_type="legal",
                master_dataset="fns_egrul",
                source="fns",
            )
            for inn in sorted(inns)
        ),
    )


def _forty_with_alan(release_id: str):
    records = forty_projections(release_id)
    alan = projection(sequence=910_000_000, release_id=release_id)
    alan = alan.model_copy(
        update={
            "company": alan.company.model_copy(
                update={
                    "inn": ALAN_INN,
                    "name": "ООО «АЛАН»",
                    "full_name": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «АЛАН»",
                }
            )
        }
    )
    retained_control = projection(sequence=910_000_001, release_id=release_id)
    retained_control = retained_control.model_copy(
        update={
            "company": retained_control.company.model_copy(
                update={
                    "inn": RETAINED_CONTROL_INN,
                    "name": "ООО «КОНТРОЛЬ»",
                    "full_name": (
                        "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «КОНТРОЛЬ»"
                    ),
                }
            )
        }
    )
    records[-2] = retained_control
    records[-1] = alan
    return sorted(records, key=lambda item: item.company.inn)


def _configure_build(
    monkeypatch,
    *,
    parent_release_id: str,
    live_records,
    target_records,
    legacy_inns: set[str] | None = None,
    inactive_inns: set[str] | None = None,
    drift_inns: set[str] | None = None,
    missing_state_inns: set[str] | None = None,
    baseline_release_overrides: dict[str, str] | None = None,
    trusted_payload_overrides: dict[str, object] | None = None,
    trusted_payload_sha256_overrides: dict[str, str] | None = None,
    missing_trusted_inns: set[str] | None = None,
    trusted_release_id: str | None = None,
    trusted_record_count: int | None = None,
    trusted_member_inns: tuple[str, ...] | None = None,
    ready_record_count: int | None = None,
):
    legacy_inns = legacy_inns or set()
    inactive_inns = inactive_inns or set()
    drift_inns = drift_inns or set()
    missing_state_inns = missing_state_inns or set()
    baseline_release_overrides = baseline_release_overrides or {}
    trusted_payload_overrides = trusted_payload_overrides or {}
    trusted_payload_sha256_overrides = trusted_payload_sha256_overrides or {}
    missing_trusted_inns = missing_trusted_inns or set()
    universe = {item.company.inn: item for item in (*live_records, *target_records)}
    companies = [
        SimpleNamespace(id=index + 1, inn=inn)
        for index, inn in enumerate(sorted(universe))
    ]
    company_by_inn = {item.inn: item for item in companies}
    live = {item.company.inn: item for item in live_records}
    target = {item.company.inn: item for item in target_records}
    states = []
    for inn, item in universe.items():
        if inn in missing_state_inns or (
            inn not in live and inn not in inactive_inns
        ):
            continue
        states.append(
            SimpleNamespace(
                company_id=company_by_inn[inn].id,
                last_published_hash=(
                    "f" * 64 if inn in drift_inns else semantic_projection_sha256(item)
                ),
                projection_version=(
                    "public-projection-v1.legacy"
                    if inn in legacy_inns
                    else PROJECTION_VERSION
                ),
                hash_algorithm_version=(
                    "sha256-canonical-json-v1"
                    if inn in legacy_inns
                    else HASH_ALGORITHM_VERSION
                ),
                is_published=inn not in inactive_inns,
                last_published_release_id=baseline_release_overrides.get(
                    inn, parent_release_id
                ),
            )
        )
    runs = {
        company_by_inn[inn].id: SimpleNamespace(
            id=uuid4(), company_id=company_by_inn[inn].id
        )
        for inn in target
    }
    manifest = _manifest(list(universe))
    monkeypatch.setattr(runner, "accepted_cohort", lambda: (manifest, "c" * 64))
    monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
    monkeypatch.setattr(runner, "cohort_companies", lambda *_args: companies)
    monkeypatch.setattr(runner, "publishable_runs", lambda *_args: runs)
    monkeypatch.setattr(service, "cohort_companies", lambda *_args: companies)
    monkeypatch.setattr(service, "publishable_runs", lambda *_args: runs)
    monkeypatch.setattr(runner, "current_main_sha", lambda: SHA)
    monkeypatch.setattr(runner, "database_url_for_psycopg", lambda: "postgresql://test")
    monkeypatch.setattr(runner.psycopg, "connect", lambda *_args, **_kwargs: _ReadOnlyConnection())
    monkeypatch.setattr(service, "database_url_for_psycopg", lambda *_args: "postgresql://test")
    monkeypatch.setattr(service.psycopg, "connect", lambda *_args, **_kwargs: _ReadOnlyConnection())
    monkeypatch.setattr(
        runner,
        "build_projection",
        lambda _cursor, inn, _publication: target[inn],
    )
    monkeypatch.setattr(
        service,
        "build_projection",
        lambda _cursor, inn, _publication: target[inn],
    )
    public_requested_inns: list[str] = []
    trusted_requested_inns: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "release_id": parent_release_id,
                    "record_count": (
                        len(live)
                        if ready_record_count is None
                        else ready_record_count
                    ),
                },
            )
        prefix = "/api/company/"
        assert request.url.path.startswith(prefix)
        inn = request.url.path.removeprefix(prefix)
        public_requested_inns.append(inn)
        if inn not in live:
            return httpx.Response(404)
        return httpx.Response(200, json=live[inn].public_payload())

    def trusted_reader(release_id: str, inns: tuple[str, ...]):
        trusted_requested_inns.extend(inns)
        projections = {
            inn: trusted_payload_overrides.get(
                inn,
                live[inn].model_dump(mode="json"),
            )
            for inn in inns
            if inn in live and inn not in missing_trusted_inns
        }
        return service.TrustedReleaseProjectionBatch(
            release_id=trusted_release_id or release_id,
            record_count=(
                len(live) if trusted_record_count is None else trusted_record_count
            ),
            member_inns=(
                tuple(sorted(live))
                if trusted_member_inns is None
                else trusted_member_inns
            ),
            projections=projections,
            payload_sha256s={
                inn: trusted_payload_sha256_overrides.get(
                    inn,
                    hashlib.sha256(canonical_json(payload)).hexdigest(),
                )
                for inn, payload in projections.items()
            },
        )

    client = httpx.Client(
        base_url="https://public.test",
        transport=httpx.MockTransport(handler),
    )
    return (
        _BuildSession(states, runs.values()),
        client,
        trusted_reader,
        public_requested_inns,
        trusted_requested_inns,
    )


def _build(monkeypatch, tmp_path: Path, *, live, target, **kwargs):
    (
        session,
        client,
        trusted_reader,
        public_requested_inns,
        trusted_requested_inns,
    ) = _configure_build(
        monkeypatch,
        parent_release_id=kwargs.pop("parent_release_id", PARENT_40),
        live_records=live,
        target_records=target,
        **kwargs,
    )
    request = _Request()
    bundle, manifest = runner.build_candidate(
        session,
        request,
        now=NOW,
        output_root=tmp_path,
        trusted_projection_reader=trusted_reader,
        client=client,
    )
    assert bundle is not None and manifest is not None
    request.public_parent_requested_inns = public_requested_inns
    request.trusted_parent_requested_inns = trusted_requested_inns
    return request, bundle, manifest


def test_incident_withdrawal_builds_39_without_stale_alan(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    retained_control = next(
        item for item in parent if item.company.inn == RETAINED_CONTROL_INN
    )

    public_view = target[0].public_payload()
    assert "schema_version" not in public_view["publication"]
    with pytest.raises(ValidationError):
        PublicProjection.model_validate(public_view)

    request, bundle, manifest = _build(
        monkeypatch,
        tmp_path,
        live=parent,
        target=target,
        drift_inns={ALAN_INN},
        trusted_payload_overrides={
            ALAN_INN: {
                "legacy_company": {"inn": ALAN_INN},
                "legacy_release": PARENT_40,
                "secret": "must-never-be-read-or-copied",
            }
        },
    )
    loaded_manifest, projections, _manifest_hash = load_bundle(bundle)

    assert manifest == loaded_manifest
    assert manifest.parent_record_count == 40
    assert manifest.record_count == 39
    assert manifest.withdrawn_count == 1
    assert [item.inn for item in manifest.withdrawn_companies] == [ALAN_INN]
    assert manifest.withdrawn_companies[0].previous_hash == "f" * 64
    assert ALAN_INN not in {item.company.inn for item in projections}
    assert request.public_parent_requested_inns == []
    assert ALAN_INN not in request.trusted_parent_requested_inns
    assert RETAINED_CONTROL_INN in request.trusted_parent_requested_inns
    assert set(request.trusted_parent_requested_inns) == {
        item.company.inn for item in target
    }
    validated_control = PublicProjection.model_validate(
        retained_control.model_dump(mode="json")
    )
    assert validated_control.company.inn == RETAINED_CONTROL_INN
    assert validated_control.publication.schema_version == "public-projection-v1"
    assert validated_control.publication.release_id == PARENT_40
    retained_hash = semantic_projection_sha256(validated_control)
    control_transition = next(
        item
        for item in manifest.unchanged_companies
        if item.inn == RETAINED_CONTROL_INN
    )
    assert control_transition.previous_hash == retained_hash
    assert control_transition.current_hash == retained_hash
    candidate_control = next(
        item for item in projections if item.company.inn == RETAINED_CONTROL_INN
    )
    assert candidate_control.company.inn == RETAINED_CONTROL_INN
    assert request.changed_company_inns == [ALAN_INN]
    assert request.change_summary[0]["transition"] == "WITHDRAWN"
    assert manifest.artifact_hashes["companies.jsonl.gz"]


def test_candidate_build_has_no_ingestion_or_external_process_side_effects(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("candidate build started a process"),
    )

    _request, _bundle, manifest = _build(
        monkeypatch, tmp_path, live=parent, target=target
    )

    assert manifest.record_count == 39


def test_identical_transition_build_is_byte_deterministic(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    _request1, first, manifest1 = _build(
        monkeypatch, first_root, live=parent, target=target
    )
    _request2, second, manifest2 = _build(
        monkeypatch, second_root, live=parent, target=target
    )

    assert manifest1 == manifest2
    for name in ("manifest.json", "companies.jsonl.gz", "checksums.sha256"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_withdrawn_company_can_be_readded_only_from_current_projection(
    monkeypatch, tmp_path
):
    original = _forty_with_alan(PARENT_40)
    parent_39_release = "public-v1-parent-39"
    parent_39 = [
        item.model_copy(
            update={
                "publication": item.publication.model_copy(
                    update={"release_id": parent_39_release}
                )
            }
        )
        for item in original
        if item.company.inn != ALAN_INN
    ]
    fresh_alan = next(item for item in original if item.company.inn == ALAN_INN)
    fresh_alan = fresh_alan.model_copy(
        update={
            "company": fresh_alan.company.model_copy(
                update={"name": "ООО «АЛАН» — АКТУАЛЬНО"}
            )
        }
    )
    target_40 = sorted([*parent_39, fresh_alan], key=lambda item: item.company.inn)

    _request, bundle, manifest = _build(
        monkeypatch,
        tmp_path,
        live=parent_39,
        target=target_40,
        parent_release_id=parent_39_release,
        inactive_inns={ALAN_INN},
    )
    _loaded, projections, _hash = load_bundle(bundle)
    alan = next(item for item in projections if item.company.inn == ALAN_INN)

    assert manifest.parent_record_count == 39
    assert manifest.record_count == 40
    assert [item.inn for item in manifest.added_companies] == [ALAN_INN]
    assert alan.company.name == "ООО «АЛАН» — АКТУАЛЬНО"


def test_withdrawal_baseline_is_promoted_only_as_absent(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    request, bundle, manifest = _build(
        monkeypatch, tmp_path, live=parent, target=target
    )
    accepted, _digest = runner.accepted_cohort()
    companies = runner.cohort_companies(None, accepted)
    company_by_inn = {item.inn: item for item in companies}
    states = [
        SimpleNamespace(
            company_id=company_by_inn[item.company.inn].id,
            last_published_hash=semantic_projection_sha256(item),
            projection_version=PROJECTION_VERSION,
            hash_algorithm_version=HASH_ALGORITHM_VERSION,
            is_published=True,
            last_published_release_id=PARENT_40,
            last_enrichment_run_id=None,
            published_at=NOW,
            updated_at=NOW,
        )
        for item in parent
    ]

    class Session:
        def scalars(self, _query):
            return states

        def get(self, *_args):
            return None

        def add(self, _item):
            pytest.fail("existing 40-row baseline unexpectedly inserted a state")

    runner._persist_published_hashes(
        Session(), request, bundle, now=NOW
    )
    alan_state = next(
        item
        for item in states
        if item.company_id == company_by_inn[ALAN_INN].id
    )

    assert alan_state.is_published is False
    assert alan_state.last_published_release_id == manifest.release_id
    assert request.status == "PUBLISHED"


def test_exact_legacy_version_upgrade_is_explicit_update(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)[:2]
    legacy_inns = {item.company.inn for item in parent}

    _request, _bundle, manifest = _build(
        monkeypatch,
        tmp_path,
        live=parent,
        target=parent,
        legacy_inns=legacy_inns,
    )

    assert manifest.projection_version == PROJECTION_VERSION
    assert manifest.hash_algorithm_version == HASH_ALGORITHM_VERSION
    assert {item.inn for item in manifest.updated_companies} == legacy_inns


def test_legacy_raw_integrity_is_independent_of_model_normalization(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)[:2]
    retained = parent[0]
    retained_inn = retained.company.inn
    legacy_raw = retained.model_dump(mode="json")
    del legacy_raw["company"]["full_name"]
    parsed = PublicProjection.model_validate(legacy_raw)

    assert parsed.company.full_name is None
    assert "full_name" in parsed.model_dump(mode="json")["company"]
    assert raw_payload_sha256(legacy_raw) != payload_sha256(parsed)

    _request, _bundle, manifest = _build(
        monkeypatch,
        tmp_path,
        live=parent,
        target=parent,
        legacy_inns={item.company.inn for item in parent},
        trusted_payload_overrides={retained_inn: legacy_raw},
    )

    assert {item.inn for item in manifest.updated_companies} == {
        item.company.inn for item in parent
    }


def test_production_shape_legacy_parent_promotes_once_without_reconciliation(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    legacy_inns = {item.company.inn for item in parent}
    legacy_raw_payloads = {}
    for item in target:
        raw_payload = item.model_dump(mode="json")
        del raw_payload["company"]["director_position"]
        legacy_raw_payloads[item.company.inn] = raw_payload
    parsed_legacy = [
        PublicProjection.model_validate(payload)
        for payload in legacy_raw_payloads.values()
    ]
    assert len(parsed_legacy) == 39
    assert all(
        raw_payload_sha256(raw_payload)
        != payload_sha256(parsed_projection)
        for raw_payload, parsed_projection in zip(
            legacy_raw_payloads.values(), parsed_legacy, strict=True
        )
    )
    (
        session,
        client,
        trusted_reader,
        public_requested,
        trusted_requested,
    ) = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        legacy_inns=legacy_inns,
        drift_inns=legacy_inns,
        trusted_payload_overrides=legacy_raw_payloads,
    )
    before = [state.__dict__.copy() for state in session.states]
    request = _Request()

    bundle, manifest = runner.build_candidate(
        session,
        request,
        now=NOW,
        output_root=tmp_path,
        trusted_projection_reader=trusted_reader,
        client=client,
    )

    assert bundle is not None and manifest is not None
    assert [state.__dict__ for state in session.states] == before
    assert manifest.parent_record_count == 40
    assert manifest.record_count == 39
    assert manifest.updated_count == 39
    assert manifest.unchanged_count == 0
    assert {item.inn for item in manifest.updated_companies} == {
        item.company.inn for item in target
    }
    assert all(item.previous_hash == "f" * 64 for item in manifest.updated_companies)
    assert [item.inn for item in manifest.withdrawn_companies] == [ALAN_INN]
    assert manifest.withdrawn_companies[0].previous_hash == "f" * 64
    assert public_requested == []
    assert ALAN_INN not in trusted_requested
    assert len(trusted_requested) == 39

    runner._persist_published_hashes(session, request, bundle, now=NOW)
    _loaded, accepted_projections, _digest = load_bundle(bundle)
    accepted_by_inn = {item.company.inn: item for item in accepted_projections}
    companies = runner.cohort_companies(None, runner.accepted_cohort()[0])
    inn_by_id = {company.id: company.inn for company in companies}
    retained_states = [
        state for state in session.states if inn_by_id[state.company_id] != ALAN_INN
    ]
    alan_state = next(
        state for state in session.states if inn_by_id[state.company_id] == ALAN_INN
    )
    assert len(retained_states) == 39
    assert all(state.projection_version == PROJECTION_VERSION for state in retained_states)
    assert all(
        state.hash_algorithm_version == HASH_ALGORITHM_VERSION
        for state in retained_states
    )
    assert all(
        state.last_published_hash
        == semantic_projection_sha256(accepted_by_inn[inn_by_id[state.company_id]])
        for state in retained_states
    )
    assert alan_state.is_published is False
    assert alan_state.last_published_hash == "f" * 64

    def second_handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url.path == "/api/ready"
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "release_id": manifest.release_id,
                "record_count": 39,
            },
        )

    def second_reader(release_id: str, inns: tuple[str, ...]):
        assert release_id == manifest.release_id
        payloads = {
            inn: accepted_by_inn[inn].model_dump(mode="json") for inn in inns
        }
        return service.TrustedReleaseProjectionBatch(
            release_id=release_id,
            record_count=39,
            member_inns=tuple(sorted(accepted_by_inn)),
            projections=payloads,
            payload_sha256s={
                inn: hashlib.sha256(canonical_json(payload)).hexdigest()
                for inn, payload in payloads.items()
            },
        )

    second_request = _Request()
    with httpx.Client(
        base_url="https://public.test",
        transport=httpx.MockTransport(second_handler),
    ) as second_client:
        second_bundle, second_manifest = runner.build_candidate(
            session,
            second_request,
            now=NOW,
            output_root=tmp_path / "second",
            trusted_projection_reader=second_reader,
            client=second_client,
        )

    assert second_bundle is None
    assert second_manifest is None
    assert second_request.status == "SUPERSEDED"
    assert second_request.changed_company_count == 0


def test_run_once_recovers_legacy_40_to_39_without_browser_projection_reads(
    monkeypatch,
    tmp_path,
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    legacy_inns = {item.company.inn for item in parent}
    legacy_payloads = {
        item.company.inn: item.model_dump(mode="json")
        for item in target
    }
    for payload in legacy_payloads.values():
        del payload["company"]["director_position"]

    (
        build_session,
        client,
        trusted_reader,
        public_requested,
        _trusted_requested,
    ) = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        legacy_inns=legacy_inns,
        drift_inns=legacy_inns,
        trusted_payload_overrides=legacy_payloads,
    )
    failed = PublicPublicationRequest(
        id=uuid4(),
        trigger_type="ENRICHMENT_READY_SCAN",
        status="FAILED",
        candidate_release_id="public-v1-failed-generation",
        previous_release_id=PARENT_40,
        changed_company_count=40,
        changed_company_ids=[],
        changed_company_inns=[item.company.inn for item in parent],
        change_summary=[],
        last_error="legacy browser payload parse failure",
        created_main_sha="c" * 40,
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    session = _BoundarySession(
        build_session.states,
        build_session.runs.values(),
        [failed],
    )
    trusted_batches: list[tuple[str, ...]] = []

    class Transport:
        def read_active_projections(self, release_id, inns):
            assert ALAN_INN not in inns
            trusted_batches.append(inns)
            return trusted_reader(release_id, inns)

    class LockConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def exec_driver_sql(self, statement, _parameters):
            return SimpleNamespace(
                scalar=lambda: statement.startswith("SELECT pg_try_advisory_lock")
            )

    class LockEngine:
        def connect(self):
            return LockConnection()

    candidate = {}
    recover_actual = runner.recover_failed_publication_if_eligible

    def recover(*args, **kwargs):
        recovered = recover_actual(*args, **kwargs)
        candidate["recovery"] = recovered
        return recovered

    def claim(_session, **_kwargs):
        replacements = [
            row
            for row in session.requests
            if row.recovered_from_request_id == failed.id
        ]
        assert replacements, {
            "requests": [
                (row.status, row.id, row.recovered_from_request_id)
                for row in session.requests
            ],
            "recovery": candidate.get("recovery"),
        }
        replacement = replacements[0]
        replacement.status = "BUILDING"
        return replacement

    def build_only(
        build_session_arg,
        request,
        *,
        output_root,
        transport,
        client,
    ):
        bundle, manifest = runner.build_candidate(
            build_session_arg,
            request,
            now=NOW,
            output_root=output_root,
            trusted_projection_reader=transport.read_active_projections,
            client=client,
        )
        candidate.update(bundle=bundle, manifest=manifest, request=request)
        return "BUILT"

    monkeypatch.setattr(runner, "engine", LockEngine())
    monkeypatch.setattr(runner, "SessionLocal", lambda: session)
    monkeypatch.setattr(runner, "recover_interrupted_requests", lambda *_args: 0)
    monkeypatch.setattr(runner, "recover_failed_publication_if_eligible", recover)
    monkeypatch.setattr(
        runner,
        "scan_public_ready_changes",
        lambda *_args: {"changed": 40, "public_ready": 39},
    )
    monkeypatch.setattr(runner, "claim_next_request", claim)
    monkeypatch.setattr(runner, "publish_claimed_request", build_only)
    monkeypatch.setattr(runner, "_resolve_public_incidents", lambda *_args, **_kwargs: None)

    result = runner.run_once(
        output_root=tmp_path,
        transport=Transport(),
        client=client,
    )

    replacement = candidate["request"]
    manifest = candidate["manifest"]
    assert result["status"] == "BUILT"
    assert replacement.recovered_from_request_id == failed.id
    assert replacement.trigger_type == service.PUBLICATION_FAILURE_RECOVERY
    assert replacement.status == "READY"
    assert manifest.record_count == 39
    assert manifest.updated_count == 39
    assert manifest.withdrawn_count == 1
    assert manifest.added_count == 0
    assert [item.inn for item in manifest.withdrawn_companies] == [ALAN_INN]
    assert public_requested == []
    assert all(ALAN_INN not in batch for batch in trusted_batches)
    assert len({inn for batch in trusted_batches for inn in batch}) == 39


@pytest.mark.parametrize("entrypoint", ["normalize", "stale_rebase"])
def test_queue_rebase_uses_trusted_legacy_parent_without_browser_reads(
    monkeypatch,
    entrypoint,
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    (
        build_session,
        client,
        trusted_reader,
        public_requested,
        trusted_requested,
    ) = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        legacy_inns={item.company.inn for item in parent},
        drift_inns={item.company.inn for item in parent},
    )
    stale = PublicPublicationRequest(
        id=uuid4(),
        trigger_type="ENRICHMENT_READY_SCAN",
        status="RETRY_SCHEDULED",
        candidate_release_id="public-v1-stale-candidate",
        previous_release_id="public-v1-stale-parent",
        changed_company_count=1,
        changed_company_ids=[],
        changed_company_inns=[ALAN_INN],
        change_summary=[],
        created_main_sha=SHA,
        created_at=NOW,
        updated_at=NOW,
    )
    session = _BoundarySession(
        build_session.states,
        build_session.runs.values(),
        [stale],
    )

    if entrypoint == "normalize":
        result = service.normalize_publication_queue(
            session,
            now=NOW,
            source_sha=SHA,
            force=True,
            trusted_projection_reader=trusted_reader,
            client=client,
        )
        assert result.dirty_count == 40
        replacement = session.get(
            PublicPublicationRequest,
            result.replacement_request_id,
        )
    else:
        assert runner._rebase_stale_candidate(
            session,
            client=client,
            trusted_projection_reader=trusted_reader,
        ) == "REBASED"
        replacement = next(
            row for row in session.requests if row.id != stale.id
        )

    assert stale.status == "SUPERSEDED"
    assert replacement.previous_release_id == PARENT_40
    assert replacement.changed_company_count == 40
    assert public_requested == []
    assert ALAN_INN not in trusted_requested
    assert len(set(trusted_requested)) == 39


def test_rollback_verification_uses_ready_and_trusted_inventory_only(monkeypatch):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    (
        session,
        client,
        trusted_reader,
        public_requested,
        trusted_requested,
    ) = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        legacy_inns={item.company.inn for item in parent},
        drift_inns={item.company.inn for item in parent},
    )

    runner._verify_rollback_parent(
        session,
        expected_release_id=PARENT_40,
        trusted_projection_reader=trusted_reader,
        client=client,
    )

    assert public_requested == []
    assert ALAN_INN not in trusted_requested
    assert len(set(trusted_requested)) == 39


def test_incomplete_bootstrap_fails_closed_without_browser_reconstruction():
    manifest = _manifest([RETAINED_CONTROL_INN])
    company = SimpleNamespace(id=1, inn=RETAINED_CONTROL_INN)

    class Session:
        def scalars(self, _query):
            return []

        def add(self, _item):
            pytest.fail("ambiguous bootstrap unexpectedly inserted state")

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"bootstrap unexpectedly called {request.url.path}")

    with httpx.Client(
        base_url="https://public.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(
            service.PublicBuildError,
            match="publication baseline bootstrap required",
        ):
            service.bootstrap_publication_state(
                Session(),
                manifest,
                [company],
                now=NOW,
                client=client,
            )


def test_mixed_current_and_legacy_parent_metadata_fails_closed(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)[:2]
    legacy_inn = parent[0].company.inn
    session, client, trusted_reader, _public_requested, trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
            legacy_inns={legacy_inn},
        )
    )

    with pytest.raises(
        service.PublicBuildError,
        match="parent publication baseline has mixed version metadata",
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    assert trusted_requested == []


def test_unknown_parent_version_pair_fails_closed(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)[:2]
    session, client, trusted_reader, _public_requested, trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
        )
    )
    for state in session.states:
        state.hash_algorithm_version = "sha256-unknown"

    with pytest.raises(
        service.PublicBuildError,
        match="parent publication baseline version pair is unsupported",
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    assert trusted_requested == []


def test_modified_stored_payload_sha_fails_closed(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)[:2]
    retained_inn = parent[0].company.inn
    session, client, trusted_reader, _public_requested, _trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
            legacy_inns={item.company.inn for item in parent},
            trusted_payload_sha256_overrides={retained_inn: "0" * 64},
        )
    )

    with pytest.raises(
        service.PublicVpsUnavailable,
        match=(
            rf"stage=transition_parent_storage inn={retained_inn} "
            r"validation_path=payload_sha256 validation_type=integrity_mismatch"
        ),
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )


def test_modified_raw_payload_with_unchanged_stored_sha_fails_closed(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)[:2]
    retained = parent[0]
    retained_inn = retained.company.inn
    original_raw = retained.model_dump(mode="json")
    modified_raw = retained.model_dump(mode="json")
    modified_raw["company"]["name"] = "ООО «ИЗМЕНЕНО»"
    session, client, trusted_reader, _public_requested, _trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
            legacy_inns={item.company.inn for item in parent},
            trusted_payload_overrides={retained_inn: modified_raw},
            trusted_payload_sha256_overrides={
                retained_inn: raw_payload_sha256(original_raw)
            },
        )
    )

    with pytest.raises(
        service.PublicVpsUnavailable,
        match=(
            rf"stage=transition_parent_storage inn={retained_inn} "
            r"validation_path=payload_sha256 validation_type=integrity_mismatch"
        ),
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )


def test_raw_integrity_failure_precedes_strict_model_validation(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)[:2]
    retained_inn = parent[0].company.inn
    session, client, trusted_reader, _public_requested, _trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
            legacy_inns={item.company.inn for item in parent},
            trusted_payload_overrides={
                retained_inn: {"not": "a PublicProjection"}
            },
            trusted_payload_sha256_overrides={retained_inn: "0" * 64},
        )
    )

    with pytest.raises(
        service.PublicVpsUnavailable,
        match=(
            rf"stage=transition_parent_storage inn={retained_inn} "
            r"validation_path=payload_sha256 validation_type=integrity_mismatch"
        ),
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )


def test_retained_invalid_trusted_payload_fails_closed_with_safe_evidence(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)[:2]
    retained_inn = parent[0].company.inn
    session, client, trusted_reader, _public_requested, _trusted_requested = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=parent,
        trusted_payload_overrides={
            retained_inn: {
                "legacy_company": {"inn": retained_inn},
                "secret": "do-not-persist-this-payload",
            }
        },
    )

    with pytest.raises(service.PublicVpsUnavailable) as caught:
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    evidence = str(caught.value)
    assert "stage=transition_parent_retained" in evidence
    assert f"inn={retained_inn}" in evidence
    assert "validation_path=" in evidence
    assert "validation_type=missing" in evidence
    assert "do-not-persist-this-payload" not in evidence


@pytest.mark.parametrize(
    ("failure", "expected_evidence"),
    [
        ("missing", "validation_type=missing"),
        ("wrong_inn", "validation_type=identity_mismatch"),
        ("wrong_release", "validation_type=release_mismatch"),
    ],
)
def test_retained_trusted_identity_and_release_requirements_fail_closed(
    monkeypatch, tmp_path, failure, expected_evidence
):
    parent = _forty_with_alan(PARENT_40)[:2]
    retained_inn = parent[0].company.inn
    missing_trusted_inns: set[str] = set()
    payload_overrides: dict[str, object] = {}
    if failure == "missing":
        missing_trusted_inns.add(retained_inn)
    elif failure == "wrong_inn":
        payload_overrides[retained_inn] = parent[1].model_dump(mode="json")
    else:
        wrong_release = parent[0].model_copy(
            update={
                "publication": parent[0].publication.model_copy(
                    update={"release_id": "public-v1-wrong-parent"}
                )
            }
        )
        payload_overrides[retained_inn] = wrong_release.model_dump(mode="json")
    session, client, trusted_reader, _public_requested, _trusted_requested = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=parent,
        trusted_payload_overrides=payload_overrides,
        missing_trusted_inns=missing_trusted_inns,
    )

    with pytest.raises(service.PublicVpsUnavailable) as caught:
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    evidence = str(caught.value)
    assert "stage=transition_parent_retained" in evidence
    assert f"inn={retained_inn}" in evidence
    assert expected_evidence in evidence


@pytest.mark.parametrize(
    ("reader_kwargs", "validation_path", "validation_type"),
    [
        (
            {"trusted_release_id": "public-v1-wrong-parent"},
            "release_id",
            "release_mismatch",
        ),
        (
            {"trusted_record_count": 1},
            "record_count",
            "count_mismatch",
        ),
    ],
)
def test_trusted_release_identity_must_match_https_ready_identity(
    monkeypatch,
    tmp_path,
    reader_kwargs,
    validation_path,
    validation_type,
):
    parent = _forty_with_alan(PARENT_40)[:2]
    session, client, trusted_reader, public_requested, _trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
            **reader_kwargs,
        )
    )

    with pytest.raises(service.PublicVpsUnavailable) as caught:
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    evidence = str(caught.value)
    assert "stage=transition_parent_storage" in evidence
    assert f"validation_path={validation_path}" in evidence
    assert f"validation_type={validation_type}" in evidence
    assert public_requested == []


def test_trusted_release_membership_must_match_persisted_parent_baseline(
    monkeypatch,
    tmp_path,
):
    parent = _forty_with_alan(PARENT_40)[:2]
    wrong_members = tuple(sorted((parent[0].company.inn, "9999999999")))
    session, client, trusted_reader, public_requested, _trusted_requested = (
        _configure_build(
            monkeypatch,
            parent_release_id=PARENT_40,
            live_records=parent,
            target_records=parent,
            trusted_member_inns=wrong_members,
        )
    )

    with pytest.raises(
        service.PublicVpsUnavailable,
        match=(
            "stage=transition_parent_storage validation_path=member_inns "
            "validation_type=membership_mismatch"
        ),
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    assert public_requested == []


def test_unexpected_hash_drift_for_current_ready_record_still_fails_closed(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)[:2]
    drift_inn = parent[0].company.inn
    session, client, trusted_reader, _public_requested, _trusted_requested = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=parent,
        drift_inns={drift_inn},
    )

    with pytest.raises(
        service.PublicBuildError,
        match=f"live projection hash is not the recorded last-good hash: {drift_inn}",
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )


def test_withdrawn_without_exact_persisted_parent_baseline_fails_closed(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    session, client, trusted_reader, public_requested, trusted_requested = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        missing_state_inns={ALAN_INN},
    )

    with pytest.raises(
        service.PublicBuildError,
        match=r"parent publication baseline count mismatch: ready=40 baseline=39",
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    assert public_requested == []
    assert trusted_requested == []


def test_parent_ready_count_must_match_authoritative_baseline(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    session, client, trusted_reader, public_requested, trusted_requested = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        ready_record_count=39,
    )

    with pytest.raises(
        service.PublicBuildError,
        match=r"parent publication baseline count mismatch: ready=39 baseline=40",
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    assert public_requested == []
    assert trusted_requested == []


def test_published_parent_baseline_release_mismatch_fails_closed(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    session, client, trusted_reader, public_requested, trusted_requested = _configure_build(
        monkeypatch,
        parent_release_id=PARENT_40,
        live_records=parent,
        target_records=target,
        baseline_release_overrides={ALAN_INN: "public-v1-not-active-parent"},
    )

    with pytest.raises(
        service.PublicBuildError,
        match=rf"published parent baseline release mismatch: {ALAN_INN}",
    ):
        runner.build_candidate(
            session,
            _Request(),
            now=NOW,
            output_root=tmp_path,
            trusted_projection_reader=trusted_reader,
            client=client,
        )

    assert public_requested == []
    assert trusted_requested == []


def test_withdrawn_route_must_be_404_during_external_verification(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    _request, bundle, manifest = _build(
        monkeypatch, tmp_path, live=parent, target=target
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "release_id": manifest.release_id,
                    "record_count": 39,
                },
            )
        if request.url.path in {
            f"/companies/{ALAN_INN}",
            f"/api/company/{ALAN_INN}",
        }:
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    with httpx.Client(
        base_url="https://public.test", transport=httpx.MockTransport(handler)
    ) as client:
        runner.verify_https_release(bundle, manifest, client=client)


def test_external_verification_compares_browser_safe_payload_without_internal_parse(
    monkeypatch,
    tmp_path,
):
    parent = _forty_with_alan(PARENT_40)[:2]
    changed = parent[0].model_copy(
        update={
            "company": parent[0].company.model_copy(
                update={"name": "ООО ИЗМЕНЁННАЯ КАРТОЧКА"}
            )
        }
    )
    target = [changed, parent[1]]
    _request, bundle, manifest = _build(
        monkeypatch,
        tmp_path,
        live=parent,
        target=target,
        legacy_inns={item.company.inn for item in parent},
    )
    _loaded, candidate_projections, _digest = load_bundle(bundle)
    by_inn = {item.company.inn: item for item in candidate_projections}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "release_id": manifest.release_id,
                    "record_count": 2,
                },
            )
        if request.url.path.startswith("/companies/"):
            return httpx.Response(200, text="ok")
        inn = request.url.path.removeprefix("/api/company/")
        payload = by_inn[inn].public_payload()
        assert "schema_version" not in payload["publication"]
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://public.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        runner.verify_https_release(bundle, manifest, client=client)


def test_public_launch_acceptance_validates_browser_contract_not_internal_model():
    item = projection(sequence=920_000_000, release_id=PARENT_40)
    payload = item.public_payload()

    launch.validate_browser_payload(payload, item.company.inn)

    invalid = {
        **payload,
        "publication": {
            **payload["publication"],
            "schema_version": "public-projection-v1",
        },
    }
    with pytest.raises(
        AssertionError,
        match="browser-safe publication metadata is invalid",
    ):
        launch.validate_browser_payload(invalid, item.company.inn)


class _PublishSession:
    def __init__(self, request):
        self.request = request

    def commit(self):
        return None

    def rollback(self):
        return None

    def get(self, _model, _identifier):
        return self.request


def _publication_request(release_id: str) -> PublicPublicationRequest:
    return PublicPublicationRequest(
        id=uuid4(),
        trigger_type="PUBLIC_COHORT_TRANSITION",
        status="READY",
        candidate_release_id=release_id,
        previous_release_id=PARENT_40,
        changed_company_count=1,
        changed_company_ids=[],
        changed_company_inns=[ALAN_INN],
        change_summary=[],
        created_main_sha=SHA,
        created_at=NOW,
        updated_at=NOW,
        attempt_count=1,
    )


def test_failed_external_acceptance_keeps_parent_active(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    _build_request, bundle, manifest = _build(
        monkeypatch, tmp_path, live=parent, target=target
    )
    request = _publication_request(manifest.release_id)
    events = []

    class Transport:
        def upload(self, *_args):
            events.append("upload")

        def import_release(self, *_args):
            events.append("stage")

        def accept_release(self, *_args):
            events.append("accept")
            raise runner.PublicTransportError(
                "acceptance", "staged acceptance failed", transient=False
            )

        def promote_release(self, *_args):
            events.append("promote")

        def rollback(self, *_args):
            events.append("rollback")

    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: PARENT_40,
    )
    monkeypatch.setattr(runner, "_active_release_id", lambda *_args: PARENT_40)
    monkeypatch.setattr(runner, "_public_incident", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_persist_published_hashes",
        lambda *_args, **_kwargs: pytest.fail("baseline changed before promotion"),
    )

    outcome = runner.publish_claimed_request(
        _PublishSession(request),
        request,
        output_root=tmp_path,
        transport=Transport(),
    )

    assert outcome == "FAILED"
    assert events == ["upload", "stage", "accept"]
    assert request.published_release_id is None


def test_successful_stage_accept_promote_updates_baseline_once(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    _build_request, _bundle, manifest = _build(
        monkeypatch, tmp_path, live=parent, target=target
    )
    request = _publication_request(manifest.release_id)
    events = []

    class Transport:
        def upload(self, *_args):
            events.append("upload")

        def import_release(self, *_args):
            events.append("stage")

        def accept_release(self, *_args):
            events.append("accept")

        def promote_release(self, *_args):
            events.append("promote")

    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: PARENT_40,
    )
    monkeypatch.setattr(
        runner,
        "verify_https_release",
        lambda *_args, **_kwargs: events.append("verify"),
    )

    def persist(_session, persisted_request, _bundle, *, now):
        events.append("persist")
        persisted_request.status = "PUBLISHED"
        persisted_request.published_release_id = manifest.release_id

    monkeypatch.setattr(runner, "_persist_published_hashes", persist)
    monkeypatch.setattr(runner, "_resolve_public_incidents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "normalize_after_successful_publication",
        lambda *_args, **_kwargs: None,
    )

    outcome = runner.publish_claimed_request(
        _PublishSession(request),
        request,
        output_root=tmp_path,
        transport=Transport(),
    )

    assert outcome == "PUBLISHED"
    assert events == ["upload", "stage", "accept", "promote", "verify", "persist"]
    assert events.count("persist") == 1


def test_post_promotion_failure_rolls_back_when_live_readback_is_unavailable(
    monkeypatch, tmp_path
):
    parent = _forty_with_alan(PARENT_40)
    target = [item for item in parent if item.company.inn != ALAN_INN]
    _build_request, _bundle, manifest = _build(
        monkeypatch, tmp_path, live=parent, target=target
    )
    request = _publication_request(manifest.release_id)
    events = []

    class Transport:
        def upload(self, *_args):
            events.append("upload")

        def import_release(self, *_args):
            events.append("stage")

        def accept_release(self, *_args):
            events.append("accept")

        def promote_release(self, *_args):
            events.append("promote")

        def rollback(self, previous_release_id):
            events.append(("rollback", previous_release_id))

        def read_active_projections(self, *_args):
            pytest.fail("trusted rollback verification was not intercepted")

    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: PARENT_40,
    )
    monkeypatch.setattr(runner, "_active_release_id", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "verify_https_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            service.PublicVpsUnavailable("candidate readback failed")
        ),
    )
    monkeypatch.setattr(runner, "_public_incident", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_verify_rollback_parent",
        lambda *_args, **kwargs: events.append(
            ("trusted-verify", kwargs["expected_release_id"])
        ),
    )

    outcome = runner.publish_claimed_request(
        _PublishSession(request),
        request,
        output_root=tmp_path,
        transport=Transport(),
    )

    assert outcome == "FAILED"
    assert events == [
        "upload",
        "stage",
        "accept",
        "promote",
        ("rollback", PARENT_40),
        ("trusted-verify", PARENT_40),
    ]
    assert request.rollback_completed_at is not None
