from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.postgres import engine
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun
from app.models.publication import PublicProjectionPublication, PublicPublicationRequest
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.services import publication_service as service
from public_app.contracts import (
    CanonicalManifest,
    ChangedCompanySummary,
    ManifestEntity,
    ReleaseManifest,
)
from scripts import run_public_sync as runner
from scripts.public_release_common import (
    canonical_json,
    semantic_projection_sha256,
    write_checksums,
)
from tests.public_test_support import forty_projections, legal_inn, projection


NOW = datetime(2026, 9, 26, 9, tzinfo=UTC)
SHA = "a" * 40


def _manifest(inns: list[str]) -> CanonicalManifest:
    return CanonicalManifest(
        schema_version="canonical-public-cohort-v1",
        manifest_version=2,
        release_name="public-sync-test",
        created_at=NOW,
        source_main_sha="b" * 40,
        source_database="nextcompany_operational",
        production_eligibility="VERIFIED_OPERATIONAL",
        selection_policy={"version": "test", "risk_outcome_used": False},
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


def _company_and_projection(session: Session, sequence: int):
    item = projection(sequence=sequence, release_id="public-v1-live")
    company = Company(
        inn=item.company.inn,
        name=item.company.name,
        short_name=item.company.name,
        full_name=item.company.full_name,
        entity_type="legal",
        ogrn=f"{sequence:013d}",
        status="ACTIVE",
        official_registry_verified=True,
    )
    session.add(company)
    session.flush()
    return company, item


def _ready_run(session: Session, company: Company) -> CompanyEnrichmentRun:
    assessment_id = str(uuid4())
    summary_id = str(uuid4())
    session.add(
        CompanyRiskAssessmentV3(
            assessment_id=assessment_id,
            company_id=company.id,
            subject_scope="legal_entity",
            risk_model_version="risk-v3",
            ruleset_version="rules-v3",
            coverage_policy_version="coverage-v3",
            applicability_policy_version="applicability-v3",
            source_resolution_policy_version="resolution-v3",
            freshness_policy_version="freshness-v3",
            input_hash=uuid4().hex + uuid4().hex,
            calculated_at=NOW,
            evidence_snapshot=[],
            resolved_checks=[],
            factors=[],
            coverage_snapshot={},
            mandatory_gate={},
            limitations=[],
            result_payload={},
        )
    )
    session.add(
        CompanySummaryV3(
            summary_id=summary_id,
            company_id=company.id,
            risk_assessment_id=assessment_id,
            summary_model_version="summary-v3",
            projection_policy_version="projection-v3",
            generated_at=NOW,
            structured_payload={},
            explainability_refs=[],
        )
    )
    run = CompanyEnrichmentRun(
        company_id=company.id,
        trigger="test",
        idempotency_key=f"public-sync-test:{uuid4()}",
        status="succeeded",
        stage="complete",
        applicable_sources=[],
        source_count=0,
        completed_source_count=0,
        failed_source_count=0,
        risk_assessment_id=assessment_id,
        summary_id=summary_id,
        public_ready=True,
        finished_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(run)
    session.flush()
    return run


def _outbox(*, created_at: datetime, inn: str) -> PublicPublicationRequest:
    return PublicPublicationRequest(
        trigger_type="ENRICHMENT_READY_SCAN",
        status="PENDING",
        changed_company_count=1,
        changed_company_ids=[],
        changed_company_inns=[inn],
        change_summary=[],
        created_main_sha=SHA,
        created_at=created_at,
        updated_at=created_at,
    )


def test_semantic_hash_ignores_release_churn_but_detects_public_content():
    first = projection(release_id="public-v1-a")
    second = first.model_copy(
        update={
            "publication": first.publication.model_copy(
                update={"release_id": "public-v1-b", "published_at": NOW}
            )
        }
    )
    changed = second.model_copy(
        update={"company": second.company.model_copy(update={"name": "ООО ИЗМЕНЕНО"})}
    )
    assert semantic_projection_sha256(first) == semantic_projection_sha256(second)
    assert semantic_projection_sha256(second) != semantic_projection_sha256(changed)
    index_changed = second.model_copy(
        update={
            "publication": second.publication.model_copy(
                update={"index_eligible": not second.publication.index_eligible}
            )
        }
    )
    assert semantic_projection_sha256(second) != semantic_projection_sha256(index_changed)


def test_three_pending_changes_coalesce_into_one_release_request():
    with Session(engine) as session:
        rows = [
            _outbox(created_at=NOW - timedelta(minutes=3), inn=legal_inn(600_000_000 + i))
            for i in range(3)
        ]
        expected_inns = {row.changed_company_inns[0] for row in rows}
        session.add_all(rows)
        session.flush()
        leader = service.claim_next_request(session, now=NOW)
        assert leader is not None
        assert leader.status == "BUILDING"
        assert leader.changed_company_count == 3
        assert set(leader.changed_company_inns) == expected_inns
        assert [row.status for row in rows].count("COALESCED") == 2
        session.rollback()


def test_restart_recovers_every_in_flight_state_without_losing_request():
    with Session(engine) as session:
        rows = []
        for status in service.RUNNING_REQUEST_STATUSES:
            row = _outbox(created_at=NOW, inn=legal_inn(610_000_000 + len(rows)))
            row.status = status
            rows.append(row)
        session.add_all(rows)
        session.flush()
        assert service.recover_interrupted_requests(session, now=NOW) == len(rows)
        assert {row.status for row in rows} == {"RETRY_SCHEDULED"}
        assert all(row.next_attempt_at == NOW for row in rows)
        session.rollback()


def test_retry_backoff_is_bounded():
    row = _outbox(created_at=NOW, inn=legal_inn(620_000_000))
    row.attempt_count = 20
    service.schedule_retry(row, RuntimeError("SSH unavailable"), now=NOW)
    assert row.status == "RETRY_SCHEDULED"
    assert row.next_attempt_at == NOW + timedelta(hours=1)


def test_advisory_lock_allows_only_one_publication_executor():
    with engine.connect() as first, engine.connect() as second:
        assert first.exec_driver_sql(
            "SELECT pg_try_advisory_lock(%s)", (service.PUBLIC_SYNC_LOCK_ID,)
        ).scalar() is True
        try:
            assert second.exec_driver_sql(
                "SELECT pg_try_advisory_lock(%s)", (service.PUBLIC_SYNC_LOCK_ID,)
            ).scalar() is False
        finally:
            first.exec_driver_sql(
                "SELECT pg_advisory_unlock(%s)", (service.PUBLIC_SYNC_LOCK_ID,)
            )


def test_bootstrap_https_outage_records_incident_without_crashing_loop(
    tmp_path, monkeypatch
):
    incidents = []
    monkeypatch.setattr(runner, "recover_interrupted_requests", lambda _session: 0)
    monkeypatch.setattr(
        runner,
        "scan_public_ready_changes",
        lambda _session: (_ for _ in ()).throw(
            service.PublicVpsUnavailable("TLS handshake timeout")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_public_incident",
        lambda _session, **kwargs: incidents.append(kwargs),
    )

    result = runner.run_once(output_root=tmp_path)

    assert result == {"status": "VPS_UNAVAILABLE", "recovered": 0, "scan": None}
    assert len(incidents) == 1
    assert incidents[0]["category"] == "PUBLIC_VPS_UNAVAILABLE"
    assert str(incidents[0]["message"]) == "TLS handshake timeout"
    assert incidents[0]["owner"] == "OUR_INFRASTRUCTURE"


def test_normalization_vps_unavailable_rolls_back_without_transport_or_hash_changes(
    tmp_path, monkeypatch
):
    request = _candidate_request("public-v1-current-candidate")
    request.id = uuid4()
    request.status = "RETRY_SCHEDULED"
    request.previous_release_id = "public-v1-last-good"
    publication = SimpleNamespace(last_published_hash="a" * 64)
    session = _TransactionalSession(request, publication)
    incidents = []
    transport_calls = []

    class Transport:
        def upload(self, *_args):
            transport_calls.append("upload")

        def import_release(self, *_args):
            transport_calls.append("import")

        def rollback(self, *_args):
            transport_calls.append("rollback")

    def normalize(_session, **_kwargs):
        request.status = "SUPERSEDED"
        request.candidate_release_id = None
        request.previous_release_id = "public-v1-wrong"
        publication.last_published_hash = "b" * 64
        raise service.PublicVpsUnavailable("ConnectTimeout")

    monkeypatch.setattr(runner, "engine", _FakeLockEngine())
    monkeypatch.setattr(runner, "SessionLocal", lambda: session)
    monkeypatch.setattr(runner, "recover_interrupted_requests", lambda _session: 0)
    monkeypatch.setattr(runner, "scan_public_ready_changes", lambda _session: {"changed": 0})
    monkeypatch.setattr(runner, "normalize_publication_queue", normalize)
    monkeypatch.setattr(
        runner,
        "_public_incident",
        lambda _session, **kwargs: incidents.append(kwargs),
    )
    monkeypatch.setattr(
        runner,
        "_resolve_public_incidents",
        lambda *_args, **_kwargs: pytest.fail("incident resolved before normalization"),
    )
    monkeypatch.setattr(
        runner,
        "claim_next_request",
        lambda *_args, **_kwargs: pytest.fail("request claimed after timeout"),
    )

    result = runner.run_once(
        output_root=tmp_path,
        transport=Transport(),
    )

    assert result == {
        "status": "VPS_UNAVAILABLE",
        "recovered": 0,
        "scan": {"changed": 0},
        "normalization": None,
    }
    assert request.status == "RETRY_SCHEDULED"
    assert request.candidate_release_id == "public-v1-current-candidate"
    assert request.previous_release_id == "public-v1-last-good"
    assert publication.last_published_hash == "a" * 64
    assert transport_calls == []
    assert len(incidents) == 1
    assert incidents[0]["category"] == "PUBLIC_VPS_UNAVAILABLE"
    assert incidents[0]["owner"] == "OUR_INFRASTRUCTURE"
    assert str(incidents[0]["message"]) == "ConnectTimeout"


class _ActiveRowsSession:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self, _query):
        return iter(self.rows)


@pytest.mark.parametrize("failure_point", ["release", "cohort"])
def test_normalization_live_lookup_timeout_does_not_mutate_request(
    monkeypatch, failure_point
):
    request = _candidate_request("public-v1-preserved")
    request.status = "RETRY_SCHEDULED"
    request.previous_release_id = "public-v1-old"
    request.next_attempt_at = NOW
    before = (
        request.status,
        request.candidate_release_id,
        request.previous_release_id,
        request.changed_company_count,
    )
    monkeypatch.setattr(
        service,
        "accepted_cohort",
        lambda: (_manifest(["0274101890"]), "c" * 64),
    )
    if failure_point == "release":
        monkeypatch.setattr(
            service,
            "fetch_live_release_id",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                service.PublicVpsUnavailable("ConnectTimeout")
            ),
        )
    else:
        monkeypatch.setattr(
            service,
            "fetch_live_release_id",
            lambda *_args, **_kwargs: "public-v1-live",
        )
        monkeypatch.setattr(
            service,
            "fetch_live_cohort",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                service.PublicVpsUnavailable("SSL timeout")
            ),
        )

    with pytest.raises(service.PublicVpsUnavailable):
        service.normalize_publication_queue(_ActiveRowsSession([request]))

    assert (
        request.status,
        request.candidate_release_id,
        request.previous_release_id,
        request.changed_company_count,
    ) == before


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_owner"),
    [
        (service.PublicBuildError("invalid cohort"), "FAILED", "OUR_CODE"),
        (RuntimeError("unexpected normalization bug"), "FAILED", "OUR_CODE"),
    ],
)
def test_normalization_code_errors_are_bounded_cycle_results(
    tmp_path, monkeypatch, error, expected_status, expected_owner
):
    request = _candidate_request("public-v1-preserved-error")
    request.id = uuid4()
    request.status = "RETRY_SCHEDULED"
    publication = SimpleNamespace(last_published_hash="a" * 64)
    session = _TransactionalSession(request, publication)
    incidents = []
    monkeypatch.setattr(runner, "engine", _FakeLockEngine())
    monkeypatch.setattr(runner, "SessionLocal", lambda: session)
    monkeypatch.setattr(runner, "recover_interrupted_requests", lambda _session: 0)
    monkeypatch.setattr(runner, "scan_public_ready_changes", lambda _session: {"changed": 0})
    monkeypatch.setattr(
        runner,
        "normalize_publication_queue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        runner,
        "_public_incident",
        lambda _session, **kwargs: incidents.append(kwargs),
    )

    result = runner.run_once(output_root=tmp_path)

    assert result["status"] == expected_status
    assert result["normalization"] is None
    assert request.status == "RETRY_SCHEDULED"
    assert incidents[0]["category"] == "PUBLIC_BUILD_ERROR"
    assert incidents[0]["owner"] == expected_owner


def test_ssh_transport_uses_explicit_pinned_host_key_and_identity(
    tmp_path, monkeypatch
):
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.touch()
    known_hosts.touch()
    monkeypatch.setenv("PUBLIC_SSH_IDENTITY_FILE", str(identity))
    monkeypatch.setenv("PUBLIC_SSH_KNOWN_HOSTS_FILE", str(known_hosts))

    transport = runner.SshPublicTransport("mikhail@195.24.64.231")

    assert transport.ssh_argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity),
    ]


def test_non_ready_accepted_and_ready_outside_cohort_do_not_enqueue(monkeypatch):
    with Session(engine) as session:
        accepted_company, accepted_projection = _company_and_projection(session, 630_000_000)
        outsider, _outsider_projection = _company_and_projection(session, 630_000_001)
        _ready_run(session, outsider)
        manifest = _manifest([accepted_company.inn])
        session.add(
            PublicProjectionPublication(
                company_id=accepted_company.id,
                last_published_hash=semantic_projection_sha256(accepted_projection),
                last_published_release_id="public-v1-live",
                published_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service,
            "bootstrap_publication_state",
            lambda *args, **kwargs: ("public-v1-live", 0),
        )
        monkeypatch.setattr(service, "current_main_sha", lambda: SHA)
        result = service.scan_public_ready_changes(session, now=NOW)
        assert result["changed"] == 0
        assert result["public_ready"] == 0
        assert session.scalar(select(PublicPublicationRequest)) is None
        session.rollback()


def test_changed_ready_accepted_company_enqueues_once(monkeypatch):
    with Session(engine) as session:
        company, live = _company_and_projection(session, 640_000_000)
        run = _ready_run(session, company)
        changed = live.model_copy(
            update={"company": live.company.model_copy(update={"name": "ООО НОВОЕ ИМЯ"})}
        )
        manifest = _manifest([company.inn])
        session.add(
            PublicProjectionPublication(
                company_id=company.id,
                last_published_hash=semantic_projection_sha256(live),
                last_published_release_id="public-v1-live",
                published_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(service, "bootstrap_publication_state", lambda *args, **kwargs: ("public-v1-live", 0))
        monkeypatch.setattr(service, "current_main_sha", lambda: SHA)
        monkeypatch.setattr(service, "build_projection", lambda *_args, **_kwargs: changed)
        first = service.scan_public_ready_changes(session, now=NOW)
        second = service.scan_public_ready_changes(session, now=NOW + timedelta(seconds=30))
        request = session.scalar(
            select(PublicPublicationRequest).where(
                PublicPublicationRequest.trigger_enrichment_run_id == run.id
            )
        )
        assert first["changed"] == 1
        assert second["changed"] == 0
        assert request.status == "PENDING"
        assert request.changed_company_inns == [company.inn]
        session.rollback()


def test_identical_ready_projection_records_no_public_change(monkeypatch):
    with Session(engine) as session:
        company, live = _company_and_projection(session, 650_000_000)
        run = _ready_run(session, company)
        manifest = _manifest([company.inn])
        session.add(
            PublicProjectionPublication(
                company_id=company.id,
                last_published_hash=semantic_projection_sha256(live),
                last_published_release_id="public-v1-live",
                published_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(service, "bootstrap_publication_state", lambda *args, **kwargs: ("public-v1-live", 0))
        monkeypatch.setattr(service, "current_main_sha", lambda: SHA)
        monkeypatch.setattr(service, "build_projection", lambda *_args, **_kwargs: live)
        result = service.scan_public_ready_changes(session, now=NOW)
        request = session.scalar(
            select(PublicPublicationRequest).where(
                PublicPublicationRequest.trigger_enrichment_run_id == run.id
            )
        )
        assert result["no_public_change"] == 1
        assert request.status == "SUPERSEDED"
        assert request.last_error == "NO_PUBLIC_CHANGE"
        session.rollback()


def _candidate_bundle(tmp_path: Path, release_id: str) -> Path:
    bundle = tmp_path / release_id
    bundle.mkdir()
    projections = forty_projections(release_id)
    with gzip.open(bundle / "companies.jsonl.gz", "wt", encoding="utf-8") as stream:
        for item in projections:
            stream.write(canonical_json(item.model_dump(mode="json")).decode() + "\n")
    manifest = ReleaseManifest(
        schema_version="public-projection-v1",
        release_id=release_id,
        source_main_sha=SHA,
        cohort_manifest_path="docs/releases/public-v1-cohort-40.json",
        cohort_manifest_sha256="c" * 64,
        cohort_source_main_sha="b" * 40,
        previous_release_id="public-v1-last-good",
        created_at=NOW,
        result_date=NOW.date(),
        content_updated_at=NOW,
        record_count=40,
        companies_file="companies.jsonl.gz",
    )
    (bundle / "manifest.json").write_bytes(
        canonical_json(manifest.model_dump(mode="json")) + b"\n"
    )
    write_checksums(bundle)
    return bundle


class _FakeSession:
    def __init__(self, request):
        self.request = request

    def commit(self):
        return None

    def rollback(self):
        return None

    def get(self, _model, _identifier):
        return self.request


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _FakeLockConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def exec_driver_sql(self, statement, _parameters):
        return _ScalarResult("pg_try_advisory_lock" in statement)


class _FakeLockEngine:
    def connect(self):
        return _FakeLockConnection()


class _TransactionalSession:
    def __init__(self, request, publication):
        self.request = request
        self.publication = publication
        self.snapshot = {
            "status": request.status,
            "candidate_release_id": request.candidate_release_id,
            "previous_release_id": request.previous_release_id,
            "last_published_hash": publication.last_published_hash,
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def commit(self):
        return None

    def rollback(self):
        self.request.status = self.snapshot["status"]
        self.request.candidate_release_id = self.snapshot["candidate_release_id"]
        self.request.previous_release_id = self.snapshot["previous_release_id"]
        self.publication.last_published_hash = self.snapshot["last_published_hash"]

    def get(self, _model, _identifier):
        return self.request


def _candidate_request(release_id: str) -> PublicPublicationRequest:
    return PublicPublicationRequest(
        trigger_type="ENRICHMENT_READY_SCAN",
        status="READY",
        candidate_release_id=release_id,
        previous_release_id="public-v1-last-good",
        changed_company_count=1,
        changed_company_ids=[],
        changed_company_inns=["0274101890"],
        change_summary=[],
        created_main_sha=SHA,
        created_at=NOW,
        updated_at=NOW,
        attempt_count=1,
    )


def test_ssh_upload_failure_schedules_retry_and_keeps_last_good(tmp_path, monkeypatch):
    release_id = "public-v1-upload-failure"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)

    class Transport:
        def upload(self, *_args):
            raise runner.PublicTransportError("upload", "SSH unavailable", transient=True)

    monkeypatch.setattr(runner, "_active_release_id", lambda _client=None: "public-v1-last-good")
    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: "public-v1-last-good",
    )
    monkeypatch.setattr(runner, "_public_incident", lambda *args, **kwargs: None)
    result = runner.publish_claimed_request(
        _FakeSession(request), request, output_root=tmp_path, transport=Transport()
    )
    assert result == "RETRY_SCHEDULED"
    assert request.status == "RETRY_SCHEDULED"
    assert request.previous_release_id == "public-v1-last-good"


def test_import_failure_fails_closed_without_switching_live_release(tmp_path, monkeypatch):
    release_id = "public-v1-import-failure"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)

    class Transport:
        def upload(self, *_args):
            return None

        def import_release(self, *_args):
            raise runner.PublicTransportError("import", "import validation failed", transient=False)

    monkeypatch.setattr(runner, "_active_release_id", lambda _client=None: "public-v1-last-good")
    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: "public-v1-last-good",
    )
    monkeypatch.setattr(runner, "_public_incident", lambda *args, **kwargs: None)
    result = runner.publish_claimed_request(
        _FakeSession(request), request, output_root=tmp_path, transport=Transport()
    )
    assert result == "FAILED"
    assert request.status == "FAILED"
    assert request.rollback_completed_at is None


def test_post_import_https_failure_rolls_back_exact_previous_release(tmp_path, monkeypatch):
    release_id = "public-v1-verify-failure"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)
    rolled_back = []

    class Transport:
        def upload(self, *_args):
            return None

        def import_release(self, *_args):
            return {"active": True, "release_id": release_id}

        def rollback(self, previous):
            rolled_back.append(previous)
            return {"active_release_id": previous}

    monkeypatch.setattr(
        runner,
        "verify_https_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            service.PublicVpsUnavailable("candidate HTTPS verification failed")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: "public-v1-last-good",
    )
    monkeypatch.setattr(runner, "_active_release_id", lambda _client=None: release_id)
    monkeypatch.setattr(runner, "accepted_cohort", lambda: (_manifest(["0274101890"]), "c" * 64))
    monkeypatch.setattr(
        runner,
        "fetch_live_cohort",
        lambda *_args, **_kwargs: ("public-v1-last-good", {}),
    )
    monkeypatch.setattr(runner, "_public_incident", lambda *args, **kwargs: None)
    result = runner.publish_claimed_request(
        _FakeSession(request), request, output_root=tmp_path, transport=Transport()
    )
    assert result == "FAILED"
    assert rolled_back == ["public-v1-last-good"]
    assert request.rollback_completed_at is not None


def _ready_client(release_id: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ready"
        return httpx.Response(
            200,
            json={"status": "ready", "release_id": release_id, "record_count": 40},
        )

    return httpx.Client(
        base_url="https://public.test",
        transport=httpx.MockTransport(handler),
    )


def _retry_candidate_row(
    *,
    release_id: str,
    previous_release_id: str,
    changed_count: int,
    sequence: int,
) -> PublicPublicationRequest:
    inns = [legal_inn(700_000_000 + sequence * 100 + index) for index in range(changed_count)]
    return PublicPublicationRequest(
        trigger_type="ENRICHMENT_READY_SCAN",
        status="RETRY_SCHEDULED",
        candidate_release_id=release_id,
        previous_release_id=previous_release_id,
        changed_company_count=changed_count,
        changed_company_ids=[],
        changed_company_inns=inns,
        change_summary=[],
        created_main_sha=SHA,
        attempt_count=3,
        next_attempt_at=NOW,
        created_at=NOW + timedelta(seconds=sequence),
        updated_at=NOW,
    )


def _normalization_companies(session: Session, count: int = 40):
    rows = []
    projections = {}
    for sequence in range(710_000_000, 710_000_000 + count):
        company, item = _company_and_projection(session, sequence)
        rows.append(company)
        projections[company.inn] = item
    return rows, projections


def _changes_for(companies: list[Company]) -> list[ChangedCompanySummary]:
    return [
        ChangedCompanySummary(
            inn=company.inn,
            previous_hash=(f"{index + 1:064x}")[-64:],
            current_hash=(f"{index + 1001:064x}")[-64:],
        )
        for index, company in enumerate(companies)
    ]


def test_retry_candidate_with_current_parent_reuses_existing_bundle(tmp_path, monkeypatch):
    release_id = "public-v1-current-parent"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)
    uploads = []

    class Transport:
        def upload(self, bundle, candidate):
            uploads.append((bundle, candidate))
            raise runner.PublicTransportError("upload", "transport disabled", transient=True)

    monkeypatch.setattr(
        runner,
        "build_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("bundle rebuilt")),
    )
    monkeypatch.setattr(runner, "_public_incident", lambda *args, **kwargs: None)
    with _ready_client("public-v1-last-good") as client:
        result = runner.publish_claimed_request(
            _FakeSession(request),
            request,
            output_root=tmp_path,
            transport=Transport(),
            client=client,
        )

    assert result == "RETRY_SCHEDULED"
    assert uploads == [(tmp_path / release_id, release_id)]


def test_stale_candidate_is_rebased_before_upload(tmp_path, monkeypatch):
    release_id = "public-v1-stale-parent"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)
    request.previous_release_id = "public-v1-old"
    uploads = []

    class Transport:
        def upload(self, *_args):
            uploads.append(True)

    def normalize(_session, **_kwargs):
        request.status = "SUPERSEDED"
        request.last_error = service.STALE_PARENT_RELEASE
        return service.PublicationQueueNormalization(
            live_release_id="public-v1-last-good",
            active_count=1,
            superseded_count=1,
            replacement_request_id=uuid4(),
            dirty_count=40,
        )

    monkeypatch.setattr(runner, "normalize_publication_queue", normalize)
    with _ready_client("public-v1-last-good") as client:
        result = runner.publish_claimed_request(
            _FakeSession(request),
            request,
            output_root=tmp_path,
            transport=Transport(),
            client=client,
        )

    assert result == "REBASED"
    assert uploads == []
    assert request.status == "SUPERSEDED"
    assert request.last_error == service.STALE_PARENT_RELEASE


def test_three_retry_candidates_normalize_to_current_truth_not_metadata(monkeypatch, tmp_path):
    with Session(engine) as session:
        companies, live = _normalization_companies(session)
        manifest = _manifest([company.inn for company in companies])
        candidates = [
            _retry_candidate_row(
                release_id=f"public-v1-old-{changed_count}",
                previous_release_id="public-v1-live",
                changed_count=changed_count,
                sequence=index,
            )
            for index, changed_count in enumerate((8, 17, 40))
        ]
        session.add_all(candidates)
        session.flush()
        for candidate in candidates:
            bundle = tmp_path / candidate.candidate_release_id
            bundle.mkdir()
            (bundle / "evidence.txt").write_text("immutable", encoding="utf-8")

        expected_changes = _changes_for(companies)
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service, "fetch_live_release_id", lambda *_args, **_kwargs: "public-v1-live"
        )
        monkeypatch.setattr(
            service,
            "fetch_live_cohort",
            lambda *_args, **_kwargs: ("public-v1-live", live),
        )
        monkeypatch.setattr(
            service,
            "_current_semantic_changes",
            lambda *_args, **_kwargs: expected_changes,
        )

        result = service.normalize_publication_queue(
            session,
            now=NOW,
            source_sha=SHA,
        )
        replacement = session.get(
            PublicPublicationRequest, result.replacement_request_id
        )

        assert result.superseded_count == 3
        assert result.dirty_count == 40
        assert replacement.status == "BUILDING"
        assert replacement.previous_release_id == "public-v1-live"
        assert replacement.changed_company_inns == [item.inn for item in expected_changes]
        assert all(candidate.status == "SUPERSEDED" for candidate in candidates)
        assert all(candidate.coalesced_into_id == replacement.id for candidate in candidates)
        assert all(
            (tmp_path / candidate.candidate_release_id / "evidence.txt").is_file()
            for candidate in candidates
        )
        session.rollback()


def test_single_stale_retry_creates_one_fresh_live_parent_request(monkeypatch):
    with Session(engine) as session:
        companies, live = _normalization_companies(session, count=1)
        manifest = _manifest([companies[0].inn])
        stale = _retry_candidate_row(
            release_id="public-v1-stale",
            previous_release_id="public-v1-old",
            changed_count=1,
            sequence=1,
        )
        session.add(stale)
        session.flush()
        changes = _changes_for(companies)
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service, "fetch_live_release_id", lambda *_args, **_kwargs: "public-v1-live"
        )
        monkeypatch.setattr(
            service,
            "fetch_live_cohort",
            lambda *_args, **_kwargs: ("public-v1-live", live),
        )
        monkeypatch.setattr(
            service,
            "_current_semantic_changes",
            lambda *_args, **_kwargs: changes,
        )

        result = service.normalize_publication_queue(session, now=NOW, source_sha=SHA)
        replacement = session.get(PublicPublicationRequest, result.replacement_request_id)
        assert stale.status == "SUPERSEDED"
        assert stale.last_error == service.STALE_PARENT_RELEASE
        assert replacement.previous_release_id == "public-v1-live"
        assert replacement.changed_company_count == 1
        session.rollback()


def test_restarted_service_revalidates_stale_ready_candidate(monkeypatch):
    with Session(engine) as session:
        companies, live = _normalization_companies(session, count=1)
        manifest = _manifest([companies[0].inn])
        stale = _retry_candidate_row(
            release_id="public-v1-stale-restart",
            previous_release_id="public-v1-old",
            changed_count=1,
            sequence=1,
        )
        stale.status = "READY"
        stale.next_attempt_at = None
        session.add(stale)
        session.flush()
        assert service.recover_interrupted_requests(session, now=NOW) == 1
        assert stale.status == "RETRY_SCHEDULED"
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service, "fetch_live_release_id", lambda *_args, **_kwargs: "public-v1-live"
        )
        monkeypatch.setattr(
            service,
            "fetch_live_cohort",
            lambda *_args, **_kwargs: ("public-v1-live", live),
        )
        monkeypatch.setattr(
            service,
            "_current_semantic_changes",
            lambda *_args, **_kwargs: _changes_for(companies),
        )

        result = service.normalize_publication_queue(session, now=NOW, source_sha=SHA)

        assert result.replacement_request_id is not None
        assert stale.status == "SUPERSEDED"
        assert stale.last_error == service.STALE_PARENT_RELEASE
        session.rollback()


def test_stale_queue_with_current_truth_equal_live_is_no_public_change(monkeypatch):
    with Session(engine) as session:
        companies, live = _normalization_companies(session, count=1)
        manifest = _manifest([companies[0].inn])
        stale = _retry_candidate_row(
            release_id="public-v1-stale-no-change",
            previous_release_id="public-v1-old",
            changed_count=1,
            sequence=1,
        )
        session.add(stale)
        session.flush()
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service, "fetch_live_release_id", lambda *_args, **_kwargs: "public-v1-live"
        )
        monkeypatch.setattr(
            service,
            "fetch_live_cohort",
            lambda *_args, **_kwargs: ("public-v1-live", live),
        )
        monkeypatch.setattr(
            service,
            "_current_semantic_changes",
            lambda *_args, **_kwargs: [],
        )

        result = service.normalize_publication_queue(session, now=NOW, source_sha=SHA)
        assert result.no_public_change is True
        assert result.replacement_request_id is None
        assert stale.status == "SUPERSEDED"
        session.rollback()


def test_live_parent_race_after_upload_prevents_import_and_rebases(tmp_path, monkeypatch):
    release_id = "public-v1-parent-race"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)
    calls = {"parent": 0, "upload": 0, "import": 0}

    class Transport:
        def upload(self, *_args):
            calls["upload"] += 1

        def import_release(self, *_args):
            calls["import"] += 1

    def validate(*_args, **_kwargs):
        calls["parent"] += 1
        if calls["parent"] == 2:
            raise runner.StaleCandidateParent(
                expected="public-v1-last-good", actual="public-v1-external"
            )
        return "public-v1-last-good"

    monkeypatch.setattr(runner, "_validated_candidate_parent", validate)
    monkeypatch.setattr(
        runner,
        "normalize_publication_queue",
        lambda *_args, **_kwargs: service.PublicationQueueNormalization(
            live_release_id="public-v1-external",
            active_count=1,
            superseded_count=1,
            replacement_request_id=uuid4(),
            dirty_count=40,
        ),
    )
    result = runner.publish_claimed_request(
        _FakeSession(request), request, output_root=tmp_path, transport=Transport()
    )

    assert result == "REBASED"
    assert calls == {"parent": 2, "upload": 1, "import": 0}


def test_successful_publication_cleanup_supersedes_satisfied_old_request(monkeypatch):
    with Session(engine) as session:
        companies, _live = _normalization_companies(session, count=1)
        manifest = _manifest([companies[0].inn])
        session.add(
            PublicProjectionPublication(
                company_id=companies[0].id,
                last_published_hash="new-live-hash",
                last_published_release_id="public-v1-new",
                published_at=NOW,
                updated_at=NOW,
            )
        )
        published = _outbox(created_at=NOW, inn=companies[0].inn)
        published.status = "PUBLISHED"
        published.published_release_id = "public-v1-new"
        obsolete = _outbox(created_at=NOW + timedelta(seconds=1), inn=companies[0].inn)
        session.add_all([published, obsolete])
        session.flush()
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service,
            "_current_semantic_changes",
            lambda *_args, **_kwargs: [],
        )

        result = service.normalize_after_successful_publication(
            session,
            published_request=published,
            now=NOW + timedelta(minutes=1),
            source_sha=SHA,
        )
        assert result.no_public_change is True
        assert obsolete.status == "SUPERSEDED"
        assert obsolete.coalesced_into_id == published.id
        session.rollback()


def test_new_enrichment_during_publish_survives_as_one_next_request(monkeypatch):
    with Session(engine) as session:
        companies, _live = _normalization_companies(session, count=1)
        manifest = _manifest([companies[0].inn])
        session.add(
            PublicProjectionPublication(
                company_id=companies[0].id,
                last_published_hash="published-hash",
                last_published_release_id="public-v1-new",
                published_at=NOW,
                updated_at=NOW,
            )
        )
        published = _outbox(created_at=NOW, inn=companies[0].inn)
        published.status = "PUBLISHED"
        published.published_release_id = "public-v1-new"
        arrived = _outbox(created_at=NOW + timedelta(seconds=1), inn=companies[0].inn)
        session.add_all([published, arrived])
        session.flush()
        changes = _changes_for(companies)
        monkeypatch.setattr(service, "accepted_cohort", lambda: (manifest, "c" * 64))
        monkeypatch.setattr(
            service,
            "_current_semantic_changes",
            lambda *_args, **_kwargs: changes,
        )

        result = service.normalize_after_successful_publication(
            session,
            published_request=published,
            now=NOW + timedelta(minutes=1),
            source_sha=SHA,
        )
        replacement = session.get(PublicPublicationRequest, result.replacement_request_id)
        assert arrived.status == "SUPERSEDED"
        assert replacement.status == "PENDING"
        assert replacement.previous_release_id == "public-v1-new"
        assert replacement.changed_company_inns == [companies[0].inn]
        session.rollback()


def test_hashes_are_persisted_only_after_https_verification(tmp_path, monkeypatch):
    release_id = "public-v1-verified-order"
    _candidate_bundle(tmp_path, release_id)
    request = _candidate_request(release_id)
    events = []

    class Transport:
        def upload(self, *_args):
            events.append("upload")

        def import_release(self, *_args):
            events.append("import")
            return {"active": True}

    monkeypatch.setattr(
        runner,
        "_validated_candidate_parent",
        lambda *_args, **_kwargs: "public-v1-last-good",
    )
    monkeypatch.setattr(
        runner,
        "verify_https_release",
        lambda *_args, **_kwargs: events.append("verify"),
    )
    monkeypatch.setattr(
        runner,
        "_persist_published_hashes",
        lambda *_args, **_kwargs: events.append("persist"),
    )
    monkeypatch.setattr(
        runner,
        "normalize_after_successful_publication",
        lambda *_args, **_kwargs: events.append("cleanup"),
    )
    monkeypatch.setattr(runner, "_resolve_public_incidents", lambda *_args, **_kwargs: None)

    result = runner.publish_claimed_request(
        _FakeSession(request), request, output_root=tmp_path, transport=Transport()
    )
    assert result == "PUBLISHED"
    assert events == ["upload", "import", "verify", "persist", "cleanup"]


def test_accepted_public_cohort_manifest_remains_exactly_40():
    manifest, _digest = service.accepted_cohort()
    assert len(manifest.entities) == 40


def test_public_vps_incident_deduplicates_across_timeout_messages(monkeypatch):
    class IncidentSession:
        incident = None

        def scalar(self, _query):
            return self.incident

    session = IncidentSession()
    upserts = []

    def upsert(_session, **kwargs):
        upserts.append(kwargs)
        session.incident = SimpleNamespace(
            updated_at=NOW,
            safe_error_message=kwargs["message"],
            resolution_evidence={},
        )
        return session.incident, True

    monkeypatch.setattr(runner, "upsert_incident", upsert)
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)

    runner._public_incident(
        session,
        category="PUBLIC_VPS_UNAVAILABLE",
        message="SSL timeout",
        owner="OUR_INFRASTRUCTURE",
    )
    runner._public_incident(
        session,
        category="PUBLIC_VPS_UNAVAILABLE",
        message="ConnectTimeout",
        owner="OUR_INFRASTRUCTURE",
    )

    assert len(upserts) == 1
    assert upserts[0]["message"] == "HOME public HTTPS verification unavailable"
    assert session.incident.safe_error_message == "ConnectTimeout"
    assert session.incident.resolution_evidence["home_verification_error"] == "ConnectTimeout"


def test_vps_recovery_resolves_incident_after_successful_normalization(
    tmp_path, monkeypatch
):
    request = _candidate_request("public-v1-recovery-candidate")
    request.id = uuid4()
    request.status = "RETRY_SCHEDULED"
    publication = SimpleNamespace(last_published_hash="a" * 64)
    session = _TransactionalSession(request, publication)
    attempts = iter(
        [
            service.PublicVpsUnavailable("ConnectTimeout"),
            service.PublicationQueueNormalization(
                live_release_id="public-v1-last-good",
                active_count=1,
            ),
        ]
    )
    incident_events = []

    def normalize(*_args, **_kwargs):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(runner, "engine", _FakeLockEngine())
    monkeypatch.setattr(runner, "SessionLocal", lambda: session)
    monkeypatch.setattr(runner, "recover_interrupted_requests", lambda _session: 0)
    monkeypatch.setattr(runner, "scan_public_ready_changes", lambda _session: {"changed": 0})
    monkeypatch.setattr(runner, "normalize_publication_queue", normalize)
    monkeypatch.setattr(runner, "claim_next_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_public_incident",
        lambda _session, **kwargs: incident_events.append(("open", kwargs["category"])),
    )
    monkeypatch.setattr(
        runner,
        "_resolve_public_incidents",
        lambda *_args, **kwargs: incident_events.append(
            ("resolve", tuple(sorted(kwargs["categories"])))
        ),
    )

    first = runner.run_once(output_root=tmp_path)
    second = runner.run_once(output_root=tmp_path)

    assert first["status"] == "VPS_UNAVAILABLE"
    assert second["status"] == "IDLE"
    assert incident_events == [
        ("open", "PUBLIC_VPS_UNAVAILABLE"),
        ("resolve", ("PUBLIC_VPS_UNAVAILABLE",)),
    ]
    assert request.status == "RETRY_SCHEDULED"
    assert request.candidate_release_id == "public-v1-recovery-candidate"


def test_persistent_loop_backs_off_then_resets_without_process_exit(tmp_path):
    results = iter(
        [
            {"status": "VPS_UNAVAILABLE"},
            {"status": "VPS_UNAVAILABLE"},
            {"status": "IDLE"},
            {"status": "IDLE"},
        ]
    )
    delays = []

    exit_code = runner._run_loop(
        once=False,
        poll_seconds=5,
        debounce_seconds=0,
        output_root=tmp_path,
        cycle=lambda **_kwargs: next(results),
        sleep=delays.append,
        max_cycles=4,
    )

    assert exit_code == 0
    assert delays == [15, 30, 5]


def test_persistent_loop_survives_unexpected_cycle_exception(
    tmp_path, monkeypatch
):
    calls = 0
    delays = []
    incidents = []

    def cycle(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("future path failed")
        return {"status": "IDLE"}

    monkeypatch.setattr(
        runner,
        "_record_unexpected_cycle_incident",
        lambda error: incidents.append(str(error)),
    )

    exit_code = runner._run_loop(
        once=False,
        poll_seconds=5,
        debounce_seconds=0,
        output_root=tmp_path,
        cycle=cycle,
        sleep=delays.append,
        max_cycles=3,
    )

    assert exit_code == 0
    assert calls == 3
    assert incidents == ["future path failed"]
    assert delays == [15, 5]


def test_once_returns_nonzero_for_unexpected_cycle_exception(
    tmp_path, monkeypatch
):
    incidents = []
    monkeypatch.setattr(
        runner,
        "_record_unexpected_cycle_incident",
        lambda error: incidents.append(str(error)),
    )

    exit_code = runner._run_loop(
        once=True,
        poll_seconds=5,
        debounce_seconds=0,
        output_root=tmp_path,
        cycle=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("once failed")),
        sleep=lambda _delay: pytest.fail("--once must not sleep"),
    )

    assert exit_code == 1
    assert incidents == ["once failed"]
