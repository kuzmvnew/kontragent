from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.incidents import controller
from app.incidents.classification import Classification, classify_failure, fingerprint
from app.incidents.engineering import (
    AgentRuntime,
    EngineeringAdapter,
    assess_diff_risk,
    branch_name,
    create_repair_package,
    detect_agent_runtime,
)
from app.incidents.safe import safe_text, safe_value
from app.models.incident import SourceIncident, SourceIncidentAction
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerJob, WorkerPublicationState, WorkerRun


NOW = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)


@pytest.fixture
def incident_db():
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


def _dataset(session, code: str, *, status: str, error: str | None = None) -> DataSet:
    source = DataSource(
        code=f"incident-test-{code}-{uuid4().hex[:8]}",
        name="Incident fixture",
        source_type="official",
        priority=1,
        enabled=True,
    )
    session.add(source)
    session.flush()
    item = DataSet(
        source_id=source.id,
        code=code,
        name=code,
        domain="fixture",
        update_mode="bulk",
        data_format="xml",
        refresh_schedule="daily",
        priority=1,
        enabled=True,
        operational_status=status,
        official_actual_until=(NOW - timedelta(days=1)).date() if status == "stale" else (NOW + timedelta(days=1)).date(),
        freshness_policy="irregular",
        auto_update_status="configured",
        last_success_at=NOW - timedelta(days=1),
        next_expected_update_at=NOW + timedelta(days=1),
        record_count=10,
        last_error=error,
        last_error_at=NOW if error else None,
    )
    session.add(item)
    session.flush()
    return item


def test_classification_distinguishes_source_owned_from_our_code():
    source_owned = classify_failure(
        source_id="fns_tax_regime",
        error_code="schema_mismatch",
        message="Official SNRIP artifact violates its XSD",
    )
    assert (source_owned.category, source_owned.owner_domain) == (
        "SOURCE_SCHEMA_VIOLATION",
        "SOURCE_OWNED",
    )
    our_code = classify_failure(
        source_id="fixture_parser",
        error_code="handler_failure",
        message="parser crashed on valid official document",
    )
    assert (our_code.category, our_code.owner_domain, our_code.remediation_level) == (
        "PARSER_ERROR",
        "OUR_CODE",
        3,
    )
    stale = classify_failure(
        source_id="cbr_warning_list", error_code=None, message=None, operational_status="stale"
    )
    assert (stale.category, stale.owner_domain) == ("SOURCE_STALE", "SOURCE_OWNED")


def test_fingerprint_deduplicates_volatile_numbers_and_persistence(incident_db):
    assert fingerprint(source_id="s", category="TIMEOUT", error_code="timeout", message="attempt 12345") == fingerprint(
        source_id="s", category="TIMEOUT", error_code="timeout", message="attempt 67890"
    )
    with incident_db() as session:
        _dataset(session, "fns_tax_regime", status="error", error="Official artifact violates XSD")
        first = controller.detect_dataset_incidents(session, now=NOW, only_source="fns_tax_regime")
        second = controller.detect_dataset_incidents(session, now=NOW, only_source="fns_tax_regime")
        session.flush()
        assert len(first) == len(second) == 1
        assert first[0].id == second[0].id
        assert first[0].category == "SOURCE_SCHEMA_VIOLATION"
        assert first[0].owner_domain == "SOURCE_OWNED"
        assert first[0].status == "WAITING_SOURCE"
        assert session.scalar(sa.select(sa.func.count()).select_from(SourceIncident)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(SourceIncidentAction)) == 2


def test_retry_policy_respects_foundation_backoff_and_retry_after(incident_db):
    with incident_db() as session:
        dataset = _dataset(session, "fns_msp", status="error", error="temporary network failure")
        source_id = "fns_msp"
        job = WorkerJob(
            source_id=source_id,
            job_type="check",
            handler_version="fixture-v1",
            idempotency_key=f"incident-{uuid4()}",
            status="retry_scheduled",
            next_attempt_at=NOW + timedelta(hours=1),
        )
        session.add(job)
        session.flush()
        run = WorkerRun(
            job_id=job.id,
            attempt_no=1,
            status="failed",
            worker_id="fixture",
            fencing_token=1,
            handler_version="fixture-v1",
            current_stage="failed",
            heartbeat_at=NOW,
            retryable=True,
            errors=[{"kind": "network_failure", "message": "temporary network failure"}],
        )
        session.add(run)
        session.flush()
        incident, _ = controller.upsert_incident(
            session,
            source_id=source_id,
            dataset=dataset.code,
            classification=Classification("TEMPORARY_NETWORK", "OUR_INFRASTRUCTURE", "MEDIUM", 1),
            error_code="network_failure",
            message="temporary network failure",
            run_id=run.id,
            job_id=job.id,
            now=NOW,
        )
        controller.process_incident(session, incident, now=NOW, force=True)
        session.flush()
        assert job.next_attempt_at == NOW + timedelta(hours=1)
        assert incident.status == "RETRY_SCHEDULED"
        assert incident.next_attempt_at == job.next_attempt_at
        assert incident.attempt_count == 1
        assert dataset.last_success_at == NOW - timedelta(days=1)


def test_backoff_circuit_breaker_and_publication_are_preserved(incident_db, monkeypatch):
    monkeypatch.setattr(controller, "run_due_updates", lambda **_kwargs: {"cbr_warning_list": "failed"})
    with incident_db() as session:
        dataset = _dataset(session, "cbr_warning_list", status="stale")
        publication = WorkerPublicationState(
            source_id="cbr_warning_list",
            active_pointer="fixture://accepted/current",
            rollback_pointer="fixture://accepted/previous",
            generation=3,
            last_fencing_token=3,
        )
        session.add(publication)
        incident, _ = controller.upsert_incident(
            session,
            source_id="cbr_warning_list",
            dataset=dataset.code,
            classification=Classification("SOURCE_STALE", "SOURCE_OWNED", "MEDIUM", 2),
            error_code=None,
            message="source is stale",
            now=NOW,
        )
        initial_success = dataset.last_success_at
        for attempt in range(3):
            controller.process_incident(session, incident, now=NOW + timedelta(hours=attempt), force=True)
        session.flush()
        assert incident.status == "AUTO_REPAIR_EXHAUSTED"
        assert incident.attempt_count == 3
        assert dataset.last_success_at == initial_success
        assert publication.active_pointer == "fixture://accepted/current"
        assert publication.generation == 3


def test_recovery_verification_requires_all_invariants(incident_db):
    with incident_db() as session:
        dataset = _dataset(session, "fns_headcount", status="current")
        publication = WorkerPublicationState(
            source_id="fns_headcount",
            active_pointer="fixture://accepted/current",
            generation=2,
            last_fencing_token=2,
        )
        session.add(publication)
        incident, _ = controller.upsert_incident(
            session,
            source_id="fns_headcount",
            dataset=dataset.code,
            classification=Classification("TIMEOUT", "OUR_INFRASTRUCTURE", "MEDIUM", 1),
            error_code="timeout",
            message="timeout",
            now=NOW,
        )
        incident.resolution_evidence = {"baseline": controller._baseline(session, incident)}
        recovered, evidence = controller.verify_recovery(session, incident, now=NOW)
        assert recovered is True
        assert all(evidence[key] for key in (
            "last_success_preserved", "publication_pointer_valid", "facts_intact",
            "scheduler_has_next_run", "error_cleared",
        ))
        dataset.last_error = "new failure"
        assert controller.verify_recovery(session, incident, now=NOW)[0] is False


def test_active_incident_recovers_without_waiting_for_cooldown(incident_db):
    with incident_db() as session:
        dataset = _dataset(session, "fns_headcount", status="current")
        publication = WorkerPublicationState(
            source_id="fns_headcount",
            active_pointer="fixture://accepted/current",
            generation=2,
            last_fencing_token=2,
        )
        session.add(publication)
        incident, _ = controller.upsert_incident(
            session,
            source_id="fns_headcount",
            dataset=dataset.code,
            classification=Classification("TIMEOUT", "OUR_INFRASTRUCTURE", "MEDIUM", 1),
            error_code="timeout",
            message="timeout",
            now=NOW,
        )
        incident.resolution_evidence = {"baseline": controller._baseline(session, incident)}
        incident.status = "RETRY_SCHEDULED"
        incident.next_attempt_at = NOW + timedelta(hours=6)
        resolved = controller.verify_active_incidents(session, now=NOW + timedelta(minutes=1))
        assert resolved == [incident]
        assert incident.status == "RESOLVED"
        assert incident.next_attempt_at is None
        session.flush()
        actions = list(session.scalars(sa.select(SourceIncidentAction).where(SourceIncidentAction.incident_id == incident.id)))
        assert [action.action_type for action in actions][-2:] == ["SOURCE_RECHECK", "RECOVERED"]


def test_worker_restart_playbook_is_fixed_argv_and_allowlisted(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    assert controller.restart_worker_service() is True
    assert calls[0][0] == ["systemctl", "--user", "restart", "nextcompany-source-worker.service"]
    assert calls[1][0] == ["systemctl", "--user", "is-active", "--quiet", "nextcompany-source-worker.service"]
    assert all("shell" not in kwargs for _, kwargs in calls)
    with pytest.raises(ValueError, match="allowlisted"):
        controller.restart_worker_service("attacker.service")


def test_lease_recovery_and_checksum_playbooks_use_canonical_boundaries(monkeypatch):
    recovered = []
    monkeypatch.setattr(controller, "recover_stale_runs", lambda *args, **kwargs: recovered.append(kwargs) or (uuid4(),))
    incident = SimpleNamespace(category="LEASE_STUCK", job_id=None, dataset_code="fns_msp")
    assert controller._execute_playbook(SimpleNamespace(), incident, now=NOW) == ("LEASE_RECOVERY", "SUCCESS")
    assert recovered and "retry_policy" in recovered[0]
    monkeypatch.setattr(controller, "run_due_updates", lambda **kwargs: {kwargs["due_codes"][0]: "success"})
    incident = SimpleNamespace(category="CHECKSUM_MISMATCH", job_id=None, dataset_code="fns_msp")
    assert controller._execute_playbook(SimpleNamespace(), incident, now=NOW) == ("SOURCE_REDISCOVERY", "SUCCESS")


def test_secret_redaction_and_no_arbitrary_shell():
    value = safe_value({
        "DATABASE_URL": "postgresql://owner:password@db/private",
        "nested": {"token": "abc", "message": "Bearer private-token", "url": "https://example.test/file?token=bad"},
    })
    assert value["DATABASE_URL"] == "[REDACTED]"
    assert value["nested"]["token"] == "[REDACTED]"
    assert "private-token" not in value["nested"]["message"]
    assert value["nested"]["url"] == "https://example.test/file"
    source = inspect.getsource(controller)
    assert "shell=True" not in source
    assert "eval(" not in source and "exec(" not in source


def test_repair_package_path_restrictions_agent_unavailable_and_branch(tmp_path, monkeypatch):
    incident = SimpleNamespace(
        incident_code="INC-20260925-ABCDEF12",
        source_id="fns_tax_regime",
        dataset_code="fns_tax_regime",
        category="PARSER_ERROR",
        owner_domain="OUR_CODE",
        severity="HIGH",
        error_code="handler_failure",
        safe_error_message="parser failed token=super-secret",
        run_id=uuid4(),
        job_id=uuid4(),
        failure_fingerprint="a" * 64,
    )
    package = create_repair_package(incident=incident, root=tmp_path, repository=tmp_path)
    assert package.parent == tmp_path.resolve()
    assert {path.name for path in package.iterdir()} == {
        "incident.json", "source_metadata.json", "validation_summary.json",
        "reproduction.txt", "required_tests.txt",
    }
    combined = "\n".join(path.read_text() for path in package.iterdir())
    assert "super-secret" not in combined
    assert "[REDACTED]" in combined
    assert branch_name(incident.incident_code, incident.source_id) == "auto/incident-20260925-abcdef12-fns-tax-regime"
    bad = SimpleNamespace(**{**incident.__dict__, "incident_code": "../escape"})
    with pytest.raises(ValueError, match="invalid incident code"):
        create_repair_package(incident=bad, root=tmp_path, repository=tmp_path)
    monkeypatch.delenv("INCIDENT_CODING_AGENT_ENABLED", raising=False)
    monkeypatch.setattr("app.incidents.engineering.shutil.which", lambda _name: "/usr/bin/codex")
    runtime = detect_agent_runtime()
    assert runtime == AgentRuntime(False, None, "AGENT_UNAVAILABLE")
    with pytest.raises(RuntimeError, match="AGENT_UNAVAILABLE"):
        EngineeringAdapter(runtime).start(incident=incident, package=package)


def test_high_risk_changes_require_review():
    assert assess_diff_risk(["migrations/versions/destructive.py"]) == "REVIEW_REQUIRED"
    assert assess_diff_risk(["public_app/main.py"]) == "REVIEW_REQUIRED"
    assert assess_diff_risk(["app/ingestion/fns_tax_regime.py"], intended_source_paths=["app/ingestion/fns_tax_regime.py"]) == "AUTO_MERGE_ELIGIBLE"
    assert assess_diff_risk(["app/unrelated.py"], intended_source_paths=["app/ingestion/fns_tax_regime.py"]) == "AWAITING_ENGINEERING_REVIEW"
