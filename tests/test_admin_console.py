from datetime import datetime, timezone
import inspect
import re
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from admin_app import main as admin_main
from admin_app import service
from admin_app import system
from app.contracts.data_readiness import AutoUpdateStatus


NOW = datetime(2026, 9, 25, 3, tzinfo=timezone.utc)


def _snapshot(*, stage="OPERATIONAL"):
    source = {
        "source_name": "ФНС: Доходы и расходы",
        "source_group": "ФНС России",
        "source_id": "fns_revenue_expenses",
        "official_url": "https://www.nalog.gov.ru/opendata/7707329152-revexp/",
        "fact_family": "revenue_expenses",
        "connected": True,
        "stage": stage,
        "enabled": True,
        "freshness": "CURRENT",
        "source_data_date": NOW.date(),
        "official_actual_until": NOW.date(),
        "last_check": NOW,
        "last_successful_run": NOW,
        "last_publication": NOW,
        "next_scheduled_check": NOW,
        "retry_at": None,
        "matched_companies": 50,
        "applicable_companies": 100,
        "master_coverage": 50.0,
        "current_fact_count": 50,
        "new_facts": 2,
        "changed_facts": 1,
        "removed_facts": 0,
        "replayed_facts": 0,
        "quarantined_records": 0,
        "publication_generation": 4,
        "last_error": None,
        "worker_status": "SUCCEEDED",
        "worker_stage": "complete",
        "latest_run_id": str(uuid4()),
        "latest_job_id": str(uuid4()),
        "retryable": False,
        "active_lease": False,
        "frequency": "daily",
        "access_channel": "bulk / xml",
        "raw_identity": "file://…/source.zip",
        "normalized_identity": "file://…/normalized.json",
        "active_checksum": "a" * 64,
        "description": "Официальный набор",
        "not_found": 50,
        "not_applicable": 0,
        "conflicts": 0,
        "runs": [],
    }
    return {
        "sources": [source],
        "source_count": 1,
        "summary": {
            "operational": int(stage == "OPERATIONAL"),
            "first_run": 0,
            "stale": int(stage == "STALE"),
            "errors": int(stage == "ERROR"),
            "blocked": 0,
            "disabled": 0,
            "queue": 0,
            "retry_scheduled": 0,
            "active_leases": 0,
        },
        "master": {"total": 100, "legal": 100, "ip": 0},
        "latest_run": None,
    }


@pytest.fixture
def client(monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr(service, "postgres_health", lambda: {"available": True, "status": "HEALTHY"})
    monkeypatch.setattr(service, "console_snapshot", lambda: snapshot)
    monkeypatch.setattr(service, "source_detail", lambda source_id: snapshot["sources"][0] if source_id == "fns_revenue_expenses" else None)
    monkeypatch.setattr(service, "change_history", lambda source_id: [])
    monkeypatch.setattr(service, "audit_rows", lambda: [])
    monkeypatch.setattr(admin_main, "systemd_status", lambda name: {"service": name, "active": True, "enabled": True, "pid": 123, "restart_count": 0, "started_at": "now"})
    monkeypatch.setattr(admin_main, "storage_status", lambda: {"raw_size": 10, "disk_free": 100, "disk_used": 20, "raw_root": "/safe/raw", "mount_identity": "device:1"})
    monkeypatch.setattr(admin_main, "list_backups", lambda limit=50: [])
    monkeypatch.setattr(admin_main, "deployed_git_sha", lambda: "a" * 40)
    return TestClient(admin_main.app)


def test_dashboard_counters_status_and_official_source_link(client):
    response = client.get("/admin/sources")
    assert response.status_code == 200
    assert "Operational" in response.text
    assert ">1<" in response.text
    assert "CONNECTED" not in response.text  # label is title-case in the table
    assert "YES" in response.text
    assert "https://www.nalog.gov.ru/opendata/7707329152-revexp/" in response.text


def test_source_detail_and_run_history_legacy_na(client, monkeypatch):
    detail = _snapshot()["sources"][0]
    detail["runs"] = [{
        "run_id": str(uuid4()), "job_id": str(uuid4()), "type": "check",
        "status": "succeeded", "started_at": NOW, "finished_at": NOW,
        "duration_ms": 1, "records_seen": 1, "matched": None, "published": 1,
        "new_facts": None, "changed_facts": None, "removed_facts": None,
        "quarantine": None, "error_code": None, "error_message": None,
        "retry_time": None, "legacy": True,
    }]
    monkeypatch.setattr(service, "source_detail", lambda _source_id: detail)
    response = client.get("/admin/sources/fns_revenue_expenses")
    assert response.status_code == 200
    assert "Последние 50 запусков" in response.text
    assert "N/A — legacy run" in response.text
    assert "file://…/source.zip" in response.text


def test_change_summary_page_explains_legacy_values(client, monkeypatch):
    row = {
        "run_id": str(uuid4()), "started_at": NOW, "source_data_date": None,
        "previous_source_data_date": None, "source_records": None, "matched": None,
        "new_facts": None, "changed_facts": None, "removed_facts": None,
        "unchanged_facts": None, "replayed_facts": None, "quarantine": None,
        "legacy": True,
    }
    monkeypatch.setattr(service, "change_history", lambda _source_id: [row])
    response = client.get("/admin/sources/fns_revenue_expenses/changes")
    assert response.status_code == 200
    assert "N/A — legacy run" in response.text
    assert "Неизвестные значения не заменяются нулём" in response.text


def test_no_get_mutation_and_csrf_is_required(client):
    assert client.get("/admin/sources/fns_revenue_expenses/actions/pause").status_code == 405
    response = client.post(
        "/admin/sources/fns_revenue_expenses/actions/pause",
        data={"_csrf": "invalid"},
    )
    assert response.status_code == 403


def test_valid_csrf_post_calls_control_once(client, monkeypatch):
    called = []
    monkeypatch.setattr(service, "perform_source_action", lambda source_id, action: called.append((source_id, action)))
    confirmation = client.get("/admin/sources/fns_revenue_expenses/confirm/pause")
    token = re.search(r'name="_csrf" value="([^"]+)"', confirmation.text).group(1)
    response = client.post(
        "/admin/sources/fns_revenue_expenses/actions/pause",
        data={"_csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert called == [("fns_revenue_expenses", "pause")]


def test_docs_and_openapi_are_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_non_loopback_clients_are_rejected():
    remote = TestClient(admin_main.app, client=("203.0.113.10", 50000))
    response = remote.get("/admin/health")
    assert response.status_code == 403
    assert response.json() == {"detail": "loopback access only"}


def test_postgres_unavailable_and_worker_offline_are_explicit(client, monkeypatch):
    monkeypatch.setattr(service, "postgres_health", lambda: {"available": False, "status": "UNAVAILABLE"})
    monkeypatch.setattr(admin_main, "systemd_status", lambda name: {"service": name, "active": False, "enabled": False, "pid": None, "restart_count": None, "started_at": None})
    response = client.get("/admin/sources")
    assert response.status_code == 200
    assert "Worker OFFLINE" in response.text
    assert "PostgreSQL недоступен" in response.text
    assert "DATABASE_URL" not in response.text


def test_status_model_distinguishes_connected_disabled_first_run_and_stale():
    dataset = SimpleNamespace(
        enabled=True,
        auto_update_status=AutoUpdateStatus.CONFIGURED,
        last_success_at=None,
        next_expected_update_at=None,
    )
    assert service._stage(dataset, connected=True, freshness="current", latest_job=None, latest_run=None, now=NOW) == "FIRST RUN"
    dataset.enabled = False
    assert service._stage(dataset, connected=True, freshness="current", latest_job=None, latest_run=None, now=NOW) == "DISABLED"
    assert service._stage(dataset, connected=True, freshness="stale", latest_job=None, latest_run=None, now=NOW) == "STALE"
    assert service._stage(dataset, connected=False, freshness="current", latest_job=None, latest_run=None, now=NOW) == "NOT CONFIGURED"


def test_s02_worker_identity_maps_to_tax_debt_dataset():
    assert service.dataset_code_for("S02") == "fns_tax_debt"
    assert service.worker_source_id_for("fns_tax_debt") == "S02"


@pytest.mark.parametrize("action,enabled", [("activate", True), ("resume", True), ("pause", False)])
def test_schedule_controls_use_canonical_configurator(monkeypatch, action, enabled):
    states = iter([
        {"enabled": not enabled, "auto_update_status": "not_configured", "latest_job_id": None},
        {"enabled": enabled, "auto_update_status": "configured" if enabled else "not_configured", "latest_job_id": None},
    ])
    monkeypatch.setattr(service, "action_state", lambda _source_id: next(states))
    called = []
    monkeypatch.setattr(service, "configure_source_schedules", lambda **kwargs: called.append(kwargs))
    monkeypatch.setattr(service, "audit_action", lambda **_kwargs: None)
    service.perform_source_action("fns_revenue_expenses", action)
    assert called == [{"enabled": enabled, "dataset_codes": ["fns_revenue_expenses"]}]


def test_check_now_uses_scheduler_and_rejects_paused_source(monkeypatch):
    state = {"enabled": True, "auto_update_status": "configured", "latest_job_id": None}
    monkeypatch.setattr(service, "action_state", lambda _source_id: state)
    called = []
    monkeypatch.setattr(service, "run_due_updates", lambda **kwargs: called.append(kwargs) or {"fns_revenue_expenses": "success"})
    monkeypatch.setattr(service, "audit_action", lambda **_kwargs: None)
    service.perform_source_action("fns_revenue_expenses", "check-now")
    assert called == [{"due_codes": ["fns_revenue_expenses"]}]

    state["enabled"] = False
    with pytest.raises(ValueError, match="paused"):
        service.perform_source_action("fns_revenue_expenses", "check-now")


def test_retry_requires_a_job_and_records_failed_audit(monkeypatch):
    state = {"enabled": True, "auto_update_status": "configured", "latest_job_id": None}
    monkeypatch.setattr(service, "action_state", lambda _source_id: state)
    audits = []
    monkeypatch.setattr(service, "audit_action", lambda **kwargs: audits.append(kwargs))
    with pytest.raises(ValueError, match="no worker job"):
        service.perform_source_action("fns_revenue_expenses", "retry")
    assert audits[-1]["result"] == "failed"


def test_secret_redaction_is_recursive_and_bounded():
    redacted = service.redact({"DATABASE_URL": "postgres://secret", "nested": {"api_key": "secret", "ok": "value", "url": "https://example.test/file?token=secret"}})
    assert redacted["DATABASE_URL"] == "[REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["ok"] == "value"
    assert redacted["nested"]["url"] == "https://example.test/file"


def test_backup_listing_rejects_arbitrary_paths(tmp_path, monkeypatch):
    good = tmp_path / "nextcompany_operational_20260925T030000Z.dump"
    bad = tmp_path / "../../etc/passwd"
    good.write_bytes(b"safe")
    (tmp_path / "not-a-backup.dump").write_text("unsafe")
    monkeypatch.setenv("OPERATIONS_BACKUP_DIR", str(tmp_path))
    rows = system.list_backups()
    assert [row["name"] for row in rows] == [good.name]
    assert "passwd" not in repr(rows)
    assert bad.name not in repr(rows)


def test_backup_helper_has_fixed_argv_and_no_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "_run", lambda argv, timeout=8, allowed_env=(): calls.append((argv, timeout, allowed_env)) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    system.invoke_fixed_helper("create_backup")
    assert calls == [(
        ["/opt/nextcompany/current/deploy/scripts/backup_operational.sh"],
        120,
        ("DATABASE_URL", "OPERATIONS_BACKUP_DIR", "OPERATIONS_RESTORE_TEST_DATABASE_URL"),
    )]
    assert "shell" not in inspect.signature(system._run).parameters
    with pytest.raises(ValueError, match="unsupported"):
        system.invoke_fixed_helper("/bin/sh")
