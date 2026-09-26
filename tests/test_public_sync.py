from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.postgres import engine
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun
from app.models.publication import PublicProjectionPublication, PublicPublicationRequest
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.services import publication_service as service
from public_app.contracts import CanonicalManifest, ManifestEntity, ReleaseManifest
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
