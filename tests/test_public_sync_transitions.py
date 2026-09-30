from __future__ import annotations

from datetime import UTC, datetime
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
from scripts import run_public_sync as runner
from scripts.public_release_common import load_bundle, semantic_projection_sha256
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
    def __init__(self, states):
        self.states = states

    def scalars(self, _query):
        return list(self.states)


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
        company_by_inn[inn].id: SimpleNamespace(id=uuid4()) for inn in target
    }
    manifest = _manifest(list(universe))
    monkeypatch.setattr(runner, "accepted_cohort", lambda: (manifest, "c" * 64))
    monkeypatch.setattr(runner, "cohort_companies", lambda *_args: companies)
    monkeypatch.setattr(runner, "publishable_runs", lambda *_args: runs)
    monkeypatch.setattr(runner, "current_main_sha", lambda: SHA)
    monkeypatch.setattr(runner, "database_url_for_psycopg", lambda: "postgresql://test")
    monkeypatch.setattr(runner.psycopg, "connect", lambda *_args, **_kwargs: _ReadOnlyConnection())
    monkeypatch.setattr(
        runner,
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
            projections={
                inn: trusted_payload_overrides.get(
                    inn,
                    live[inn].model_dump(mode="json"),
                )
                for inn in inns
                if inn in live and inn not in missing_trusted_inns
            },
        )

    client = httpx.Client(
        base_url="https://public.test",
        transport=httpx.MockTransport(handler),
    )
    return (
        _BuildSession(states),
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


def test_live_parent_membership_uses_ready_count_and_404_not_manifest_size():
    records = _forty_with_alan(PARENT_40)[:2]
    manifest = _manifest([item.company.inn for item in records])
    retained = records[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "release_id": PARENT_40,
                    "record_count": 1,
                },
            )
        if request.url.path == f"/api/company/{retained.company.inn}":
            return httpx.Response(200, json=retained.model_dump(mode="json"))
        return httpx.Response(404)

    with httpx.Client(
        base_url="https://public.test", transport=httpx.MockTransport(handler)
    ) as client:
        release_id, live = service.fetch_live_cohort(manifest, client=client)

    assert release_id == PARENT_40
    assert list(live) == [retained.company.inn]


def test_default_live_cohort_reader_remains_strict_with_safe_schema_evidence():
    retained = _forty_with_alan(PARENT_40)[0]
    manifest = _manifest([retained.company.inn])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "release_id": PARENT_40,
                    "record_count": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "legacy_company": {"inn": retained.company.inn},
                "secret": "strict-reader-must-not-log-payload",
            },
        )

    with httpx.Client(
        base_url="https://public.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(service.PublicVpsUnavailable) as caught:
            service.fetch_live_cohort(manifest, client=client)

    evidence = str(caught.value)
    assert "stage=strict_live_cohort" in evidence
    assert f"inn={retained.company.inn}" in evidence
    assert "validation_path=" in evidence
    assert "validation_type=missing" in evidence
    assert "strict-reader-must-not-log-payload" not in evidence


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


def test_schema_version_upgrade_is_explicit_update(monkeypatch, tmp_path):
    parent = _forty_with_alan(PARENT_40)[:2]
    legacy_inn = parent[0].company.inn

    _request, _bundle, manifest = _build(
        monkeypatch,
        tmp_path,
        live=parent,
        target=parent,
        legacy_inns={legacy_inn},
    )

    assert manifest.projection_version == PROJECTION_VERSION
    assert manifest.hash_algorithm_version == HASH_ALGORITHM_VERSION
    assert [item.inn for item in manifest.updated_companies] == [legacy_inn]


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
        "accepted_cohort",
        lambda: (_manifest([item.company.inn for item in parent]), "c" * 64),
    )
    monkeypatch.setattr(
        runner,
        "fetch_live_cohort",
        lambda *_args, **_kwargs: (PARENT_40, {}),
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
    ]
    assert request.rollback_completed_at is not None
