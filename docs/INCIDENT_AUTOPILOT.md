# NEXT Company Incident Autopilot

The controller is a separate persistent service. It detects current DataSet and WorkerRun failures, deduplicates them by a stable fingerprint, applies only fixed playbooks, and verifies recovery without changing the last successful publication.

## Owner URLs

- Incidents: `http://127.0.0.1:8081/admin/incidents`
- Policies: `http://127.0.0.1:8081/admin/automation`
- Sources: `http://127.0.0.1:8081/admin/sources`

From macOS, start the same private tunnel used by the Source Operations Console:

```bash
ssh -N -L 8081:127.0.0.1:8081 -p 2222 mikhail@nextlinux
```

## Automation levels

- L0 observes only.
- L1 respects Worker Foundation retry state and its scheduled backoff/Retry-After time.
- L2 uses fixed source rediscovery, stale-lease recovery and worker-service restart playbooks.
- L3 is enabled only when an explicitly authorized coding-agent adapter is installed. Otherwise the UI reports `AGENT_UNAVAILABLE`; L1/L2 continue normally.

`WAITING_SOURCE` means validation identified an official-source problem. The controller preserves the accepted publication and schedules bounded rechecks. It does not weaken XML/XSD/checksum validation or patch official RAW bytes.

## Safe controls

Every mutation uses a confirmation page, POST, CSRF validation and the admin audit log. Policy values are chosen from fixed max-attempt and cooldown sets. No command, path, SQL or payload can be supplied by the browser.

## Service checks

```bash
systemctl --user status nextcompany-incident-controller.service
journalctl --user -u nextcompany-incident-controller.service --since today
```

The service runs `scripts.run_incident_controller` from `/home/mikhail/nextcompany-runtime/current`, restarts on failure, and stores repair packages under `/home/mikhail/nextcompany-operational/incidents/<incident-code>/` with mode `0700/0600`.

## Backup gate before migration

The backup helper supports a fixed Docker restore rehearsal when `OPERATIONS_RESTORE_TEST_DATABASE` is set to a non-production database name. A failed restore returns a non-zero status and writes `FAIL`; deployment must stop before migration. The rehearsal database is dropped after verification.
