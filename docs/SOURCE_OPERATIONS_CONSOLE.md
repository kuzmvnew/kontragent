# Source Operations Console

The console is private and manages only HOME DATA WORKER. It is not part of
`nextcompany.pro`, has no public route, and rejects every non-loopback client in
the application as well as binding Uvicorn to `127.0.0.1`.

## Open it

On the Windows worker:

```text
http://127.0.0.1:8081/admin/sources
```

From Mac, keep this tunnel running:

```bash
ssh -N -L 8081:127.0.0.1:8081 -p 2222 mikhail@nextlinux
```

Then open `http://127.0.0.1:8081/admin/sources`.

Never forward port 8081 from the router and never change the service bind to
`0.0.0.0`. Any future non-loopback deployment requires private authentication
and a separate security review.

## Read the status

- Green `OPERATIONAL`: connected runnable integration, enabled schedule and a
  current successful publication.
- Yellow `FIRST RUN`, `CHECK PENDING` or `STALE`: owner attention or a scheduled
  check is required, but this is not automatically a parser failure.
- Red `ERROR`, `SOURCE BLOCKED` or `ACCESS REQUIRED`: inspect the latest run and
  its safe error envelope.
- Gray `DISABLED` or `NOT CONFIGURED`: no automatic checks are running.
- `CONNECTED YES` means both executable scheduler code and an enabled durable
  handler registration exist. Code presence alone is not reported as connected.

## Controls

`CHECK NOW` asks the canonical scheduler to discover/check the official release.
It does not call a parser directly and does not force-download an unchanged file.

`PAUSE SCHEDULE` stops future automatic checks without deleting facts, RAW,
publication pointers or the previous successful state. `RESUME SCHEDULE` makes
the source due again. `RETRY FAILED JOB` is available only for an already
retry-scheduled, retryable Worker Foundation job with no active lease.

Every change uses a confirmation page, POST, CSRF validation and an audit entry
under `/admin/audit`. Worker restart is display-only in v1 because no browser
privilege boundary is installed.

## What changed and run history

Open a source, then `VIEW CHANGES` for canonical per-run counters. `N/A — legacy
run` means that the older run did not persist that metric; it is never presented
as zero. The source page lists the last 50 WorkerRuns. A run page shows bounded,
redacted metadata and RAW manifest identity, never the private RAW payload.

## Service checks

```bash
systemctl --user status nextcompany-admin.service
systemctl --user status nextcompany-source-worker.service
curl --fail http://127.0.0.1:8081/admin/health
```

The System page shows service PID/restarts, PostgreSQL health, queue/leases,
storage and deployed SHA. `CREATE BACKUP` executes one fixed, root-owned release
script with no browser-supplied path or arguments.
