# V1 Worker Foundation runbook

Status: foundation contract only. No production scheduler, source handler,
object storage, alert delivery, backup backend, facts publication, Risk,
Summary, API or Card integration is enabled by DEV-008.

## Runtime safety boundary

- `main.py` does not import the worker package.
- The handler registry starts empty and rejects live handlers.
- A job can run only when the exact `(source_id, handler_version)` exists in
  both the process registry and the durable approval registry.
- PostgreSQL leases are authoritative. A local mutex or process lock must
  never be used as the execution authority.
- Fencing tokens come from `worker_lease_fencing_token_seq`; deleting or
  completing a lease cannot reset or reuse a token.
- Handlers run in spawned child processes. The parent owns database state,
  heartbeat renewal, timeout enforcement and child termination/kill cleanup.
- Publication changes only an isolated foundation pointer. It does not write
  facts or invoke Risk, Summary, Public Projection, API or Card code.

## Stale-run recovery placeholder

1. Detect `running` rows whose heartbeat is stale.
2. Confirm that the execution deadline or PostgreSQL lease expiry has passed.
3. Mark the run `timed_out` under a row lock.
4. Schedule a retry only while the job has attempts remaining.
5. Require a newly acquired lease and a larger fencing token for the retry.
6. Verify that the previous publication pointer remains active.

Acceptance points:

- the stale run has a terminal timestamp and duration;
- its error list records `timeout`;
- a retry is scheduled only under the explicit retry policy;
- an expired owner cannot heartbeat or publish;
- monitoring can distinguish stale, timed-out, failed and succeeded runs.
- execution counters retain the last parent-persisted progress report even
  when the child times out or fails.

## Handler execution boundary

- Registered handler callables must be serializable by Python's `spawn`
  multiprocessing context.
- Handler results, RAW references, validation results and execution counters
  cross a narrow IPC pipe; handlers do not mutate worker database state.
- The parent periodically renews the lease while supervising the child.
- At `timeout_seconds`, the parent terminates the child, escalates to kill if
  it does not exit within the cleanup window, and records `timed_out`.
- Graceful shutdown stops new claims. An in-flight child may observe
  `context.shutdown_requested()` and return cleanly before its timeout.

## Backup and restore placeholder

The `BackupRecoveryBackend` protocol is the only backup integration supplied
by DEV-008. A future production runbook must define the backup implementation,
retention, encryption, access control and recovery objective before enablement.

Required restore acceptance points:

- restore is performed into an isolated staging target;
- worker job/run history and fencing tokens are retained;
- immutable RAW manifest references and checksums are consistent;
- active and rollback publication pointers resolve;
- referential constraints and worker indexes pass verification;
- restoring data does not enable a scheduler or live handler;
- publication requires a separate validated atomic pointer transition.

## Migration recovery

For the foundation migration, acceptance is:

1. upgrade from the previous Alembic revision;
2. inspect constraints and indexes;
3. downgrade exactly one revision;
4. upgrade to head again;
5. run the worker-foundation tests.

Production backup and recovery execution are explicitly outside DEV-008.
