import inspect
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from admin_app import main as admin_main
from admin_app import service, system
from admin_app.presentation import format_date, format_datetime
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
        "auto_repair": "ON",
        "open_incident_id": None,
        "open_incident_code": None,
        "description": "Официальный набор",
        "not_found": 50,
        "not_applicable": 0,
        "conflicts": 0,
        "runs": [],
        "missing_reasons": {
            field: "Нет данных: тестовая причина."
            for field in (
                "source_data_date", "official_actual_until", "last_check",
                "last_successful_run", "last_publication", "next_scheduled_check",
                "retry_at", "matched_companies", "master_coverage",
                "current_fact_count", "new_facts", "changed_facts",
                "removed_facts", "replayed_facts", "quarantined_records",
                "publication_generation",
            )
        },
    }
    return {
        "sources": [source],
        "source_count": 1,
        "data_processes": 1,
        "connected": 1,
        "source_families": 1,
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
        "enrichment": {
            "companies_not_started": 90,
            "companies_in_progress": 5,
            "companies_complete": 5,
            "risk_ready_companies": 4,
            "summary_ready_companies": 3,
            "public_ready_companies": 2,
            "coverage_at_least_1": 10,
            "coverage_at_least_3": 8,
            "coverage_at_least_5": 6,
            "coverage_at_least_10": 1,
            "coverage_100_percent": 5,
            "average_coverage_percent": 7.5,
            "median_coverage_percent": 0.0,
        },
        "incidents": {"open": 0, "running": 0, "waiting_source": 0, "review_required": 0, "recovered_today": 0, "exhausted": 0},
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
    assert "DATA PROCESSES" in response.text
    assert "CONNECTED" in response.text
    assert "OPERATIONAL" in response.text
    assert "SOURCE FAMILIES" in response.text
    assert "MASTER COMPANIES" in response.text
    assert "FULLY ENRICHED" in response.text
    assert ">1<" in response.text
    assert "Connected" not in response.text
    assert "ДА" in response.text
    assert "data-tooltip" in response.text
    assert "https://www.nalog.gov.ru/opendata/7707329152-revexp/" in response.text


def test_source_inventory_distinguishes_processes_handlers_and_families():
    counts = service._source_inventory_counts(
        [
            {"connected": True, "source_group": "FNS", "fact_family": "tax"},
            {"connected": False, "source_group": "FNS", "fact_family": "tax"},
            {"connected": True, "source_group": "FNS", "fact_family": "msp"},
            {"connected": True, "source_group": "CBR", "fact_family": "warning"},
        ]
    )
    assert counts == {
        "data_processes": 4,
        "connected": 3,
        "source_families": 3,
    }


def test_generic_operational_gate_requires_second_accepted_check():
    dataset = SimpleNamespace(
        enabled=True,
        auto_update_status=AutoUpdateStatus.CONFIGURED,
        last_success_at=NOW,
        next_expected_update_at=NOW + timedelta(days=1),
        coverage={"successful_scheduled_checks": 1, "operational_accepted": False},
    )
    assert service._stage(
        dataset,
        connected=True,
        freshness="current",
        latest_job=None,
        latest_run=None,
        now=NOW,
    ) == "FIRST RUN"
    dataset.coverage["successful_scheduled_checks"] = 2
    dataset.coverage["operational_accepted"] = True
    assert service._stage(
        dataset,
        connected=True,
        freshness="current",
        latest_job=None,
        latest_run=None,
        now=NOW,
    ) == "OPERATIONAL"


def test_source_detail_explains_legacy_missing_metrics(client, monkeypatch):
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
    assert "N/A" not in response.text
    assert "Нет данных" in response.text
    assert "старый WorkerRun не сохранял эту метрику" in response.text
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
    assert "N/A" not in response.text
    assert "старый WorkerRun не сохранял эту метрику" in response.text
    assert "Неизвестные значения не заменяются нулём" in response.text


def test_no_get_mutation_and_csrf_is_required(client):
    assert client.get("/admin/sources/fns_revenue_expenses/actions/pause").status_code == 405
    response = client.post(
        "/admin/sources/fns_revenue_expenses/actions/pause",
        data={"_csrf": "invalid"},
    )
    assert response.status_code == 403


def test_csrf_accepts_browser_null_origin_but_rejects_foreign_origin(client, monkeypatch):
    called = []
    monkeypatch.setattr(
        service,
        "perform_source_action",
        lambda source_id, action: called.append((source_id, action)),
    )
    confirmation = client.get("/admin/sources/fns_revenue_expenses/confirm/pause")
    token = re.search(r'name="_csrf" value="([^"]+)"', confirmation.text).group(1)

    accepted = client.post(
        "/admin/sources/fns_revenue_expenses/actions/pause",
        data={"_csrf": token},
        headers={"Origin": "null"},
        follow_redirects=False,
    )
    rejected = client.post(
        "/admin/sources/fns_revenue_expenses/actions/pause",
        data={"_csrf": token},
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert rejected.status_code == 403
    assert called == [("fns_revenue_expenses", "pause")]


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
    assert "Worker ОСТАНОВЛЕН" in response.text
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
        ("DATABASE_URL", "OPERATIONS_BACKUP_DIR", "OPERATIONS_RESTORE_TEST_DATABASE_URL", "OPERATIONS_RESTORE_TEST_DATABASE"),
    )]
    assert "shell" not in inspect.signature(system._run).parameters
    with pytest.raises(ValueError, match="unsupported"):
        system.invoke_fixed_helper("/bin/sh")


def _incident():
    incident_id = uuid4()
    return {
        "id": str(incident_id),
        "incident_code": "INC-20260925-ABCDEF12",
        "source_id": "fns_tax_regime",
        "dataset_code": "fns_tax_regime",
        "run_id": None,
        "job_id": None,
        "severity": "HIGH",
        "category": "SOURCE_SCHEMA_VIOLATION",
        "owner_domain": "SOURCE_OWNED",
        "status": "WAITING_SOURCE",
        "detected_at": NOW,
        "updated_at": NOW,
        "resolved_at": None,
        "error_code": "schema_mismatch",
        "safe_error_message": "Official artifact failed XML/XSD validation",
        "failure_fingerprint": "a" * 64,
        "auto_heal_enabled": True,
        "auto_code_repair_enabled": False,
        "remediation_level": 2,
        "attempt_count": 1,
        "max_attempts": 3,
        "next_attempt_at": NOW,
        "cooldown_until": NOW,
        "last_action": "SOURCE_REDISCOVERY",
        "last_action_result": "FAILED",
        "repair_branch": None,
        "repair_pr_number": None,
        "repair_commit_sha": None,
        "repair_ci_status": "NOT_RUN",
        "resolution": None,
        "resolution_evidence": {"last_success_preserved": True},
        "actions": [],
        "available_actions": ("check-source-now", "pause-auto-repair"),
        "automation_paused": False,
        "source_check_count": 1,
        "last_source_check": NOW,
        "last_source_check_result": "SOURCE_STILL_INVALID",
        "problem_summary": "Официальный файл не проходит обязательную проверку схемы.",
        "system_action_summary": "Ждёт исправленный официальный файл и проверяет источник автоматически.",
        "owner_action_summary": "Ничего делать не требуется.",
        "agent_available": False,
    }


def test_incidents_and_automation_pages_show_safe_state(client, monkeypatch):
    incident = _incident()
    agent = SimpleNamespace(available=False, status="AGENT_UNAVAILABLE", agent_type=None)
    monkeypatch.setattr(service, "incident_rows", lambda: {
        "rows": [incident],
        "summary": {"open": 1, "running": 0, "waiting_source": 1, "review_required": 0, "recovered_today": 0, "exhausted": 0},
        "agent": agent,
        "notification_status": "NOTIFICATION CHANNEL NOT CONFIGURED",
    })
    monkeypatch.setattr(service, "incident_detail", lambda incident_id: incident if str(incident_id) == incident["id"] else None)
    monkeypatch.setattr(service, "automation_rows", lambda: {
        "rows": [{
            "source_id": "fns_tax_regime", "dataset_code": "fns_tax_regime",
            "auto_heal_enabled": True, "auto_code_repair_enabled": False,
            "remediation_level": 2, "max_attempts": 3, "cooldown_seconds": 3600,
            "paused": False, "circuit_breaker": "1/3", "current_incident": incident,
        }],
        "agent": agent,
        "notification_status": "NOTIFICATION CHANNEL NOT CONFIGURED",
        "max_attempt_values": tuple(range(1, 11)),
        "cooldown_values": (60, 300, 900, 3600, 21600, 86400),
    })
    incidents = client.get("/admin/incidents")
    assert incidents.status_code == 200
    assert "SOURCE_SCHEMA_VIOLATION" in incidents.text
    assert "SOURCE_OWNED" in incidents.text
    assert "Автоматическое исправление кода: НЕДОСТУПНО" in incidents.text
    detail = client.get(f"/admin/incidents/{incident['id']}")
    assert detail.status_code == 200
    assert "Official artifact failed XML/XSD validation" in detail.text
    automation = client.get("/admin/automation")
    assert automation.status_code == 200
    assert "Канал внешних уведомлений не настроен" in automation.text
    assert "1/3" in automation.text


def test_incident_actions_require_csrf_confirmation_and_audit_boundary(client, monkeypatch):
    incident = _incident()
    monkeypatch.setattr(service, "incident_detail", lambda incident_id: incident if str(incident_id) == incident["id"] else None)
    called = []
    monkeypatch.setattr(service, "perform_incident_action", lambda incident_id, action: called.append((str(incident_id), action)) or incident)
    path = f"/admin/incidents/{incident['id']}"
    assert client.get(f"{path}/actions/check-source-now").status_code == 405
    assert client.post(f"{path}/actions/check-source-now", data={"_csrf": "bad"}).status_code == 403
    assert client.get(f"{path}/confirm/request-engineering").status_code == 404
    confirmation = client.get(f"{path}/confirm/check-source-now")
    assert "Проверить официальный источник сейчас?" in confirmation.text
    token = re.search(r'name="_csrf" value="([^"]+)"', confirmation.text).group(1)
    response = client.post(
        f"{path}/actions/check-source-now",
        data={"_csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Запрос принят" in response.text
    assert "Ожидает выполнения" in response.text
    assert called == [(incident["id"], "check-source-now")]


def test_automation_policy_post_is_csrf_protected_and_predefined(client, monkeypatch):
    agent = SimpleNamespace(available=False, status="AGENT_UNAVAILABLE", agent_type=None)
    context = {
        "rows": [{
            "source_id": "fns_tax_regime", "dataset_code": "fns_tax_regime",
            "auto_heal_enabled": True, "auto_code_repair_enabled": False,
            "remediation_level": 2, "max_attempts": 3, "cooldown_seconds": 3600,
            "paused": False, "circuit_breaker": "CLOSED", "current_incident": None,
        }],
        "agent": agent,
        "notification_status": "NOTIFICATION CHANNEL NOT CONFIGURED",
        "max_attempt_values": tuple(range(1, 11)),
        "cooldown_values": (60, 300, 900, 3600, 21600, 86400),
    }
    monkeypatch.setattr(service, "automation_rows", lambda: context)
    called = []
    monkeypatch.setattr(service, "perform_policy_update", lambda **kwargs: called.append(kwargs))
    assert client.post("/admin/automation/fns_tax_regime/update", data={"_csrf": "bad"}).status_code == 403
    confirmation = client.get("/admin/automation/fns_tax_regime/confirm")
    token = re.search(r'name="_csrf" value="([^"]+)"', confirmation.text).group(1)
    response = client.post(
        "/admin/automation/fns_tax_regime/update",
        data={"_csrf": token, "auto_heal": "on", "max_attempts": "3", "cooldown_seconds": "3600"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert called == [{
        "source_id": "fns_tax_regime", "auto_heal_enabled": True,
        "auto_code_repair_enabled": False, "max_attempts": 3, "cooldown_seconds": 3600,
    }]


def test_incident_action_eligibility_matrix_is_server_side():
    enabled = SimpleNamespace(paused=False, auto_heal_enabled=True)
    paused = SimpleNamespace(paused=True, auto_heal_enabled=True)
    unavailable = SimpleNamespace(available=False)
    available = SimpleNamespace(available=True)
    source_owned = _incident()
    assert service.available_incident_actions(source_owned, enabled, unavailable) == (
        "check-source-now", "pause-auto-repair",
    )
    assert service.available_incident_actions(source_owned, paused, unavailable) == (
        "resume-auto-repair",
    )
    our_code = {
        **source_owned,
        "owner_domain": "OUR_CODE",
        "category": "PARSER_ERROR",
        "status": "OPEN",
    }
    assert "prepare-repair-package" in service.available_incident_actions(our_code, enabled, unavailable)
    assert "request-engineering" not in service.available_incident_actions(our_code, enabled, unavailable)
    assert "request-engineering" in service.available_incident_actions(our_code, enabled, available)
    infrastructure = {
        **source_owned,
        "owner_domain": "OUR_INFRASTRUCTURE",
        "category": "WORKER_NOT_RUNNING",
        "status": "OPEN",
    }
    assert service.available_incident_actions(infrastructure, enabled, unavailable) == (
        "run-auto-heal", "pause-auto-repair", "cancel-pending",
    )
    assert service.available_incident_actions({**source_owned, "status": "RESOLVED"}, enabled, unavailable) == ()


def test_russian_datetime_formatter_uses_home_worker_timezone():
    assert format_datetime(NOW) == "25.09.2026 06:00"
    assert format_date(NOW.date()) == "25.09.2026"
    assert "T" not in format_datetime(NOW)
    assert "+00:00" not in format_datetime(NOW)


def test_dashboard_backup_tile_is_compact_and_hides_sha(client, monkeypatch):
    monkeypatch.setattr(admin_main, "list_backups", lambda limit=50: [{
        "timestamp": NOW,
        "sha256": "b" * 64,
        "restore_test": {"status": "PASS", "checked_at": NOW.isoformat()},
    }])
    response = client.get("/admin/sources")
    assert response.status_code == 200
    assert "25.09.2026 06:00" in response.text
    assert "bbbbbbbbbbbb" not in response.text
    assert NOW.isoformat() not in response.text


def test_zero_is_information_and_missing_value_has_reason(client, monkeypatch):
    snapshot = _snapshot()
    snapshot["sources"][0]["matched_companies"] = 0
    snapshot["sources"][0]["master_coverage"] = 0.0
    snapshot["sources"][0]["new_facts"] = None
    monkeypatch.setattr(service, "console_snapshot", lambda: snapshot)
    response = client.get("/admin/sources")
    assert response.status_code == 200
    assert ">0<" in response.text
    assert "0.0%" in response.text
    assert "Нет данных" in response.text
    assert "data-tooltip=\"Нет данных: тестовая причина.\"" in response.text
    assert "N/A" not in response.text


def test_source_headers_have_hover_help(client):
    response = client.get("/admin/sources")
    assert response.status_code == 200
    for label in ("Подключён", "Состояние", "Автовосстановление", "Последняя проверка", "Покрытие Master"):
        assert re.search(rf'data-tooltip="[^"]+">{label}', response.text)
