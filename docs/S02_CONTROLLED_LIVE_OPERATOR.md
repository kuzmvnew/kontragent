# S02 controlled-live operator

`scripts/run_s02_controlled_live.py` is the canonical, fail-closed operator
entrypoint for one bounded FNS tax-debt Run A. It only coordinates accepted
S02 and worker functions. It does not implement parsing, normalization,
publication, Risk, Summary, scheduling, or mass ingestion.

The launcher is disabled by default at the product level:

- `MASS_INGESTION_ENABLED=False`
- `CONTROLLED_LIVE_PILOT_ENABLED=False`
- no production scheduler invokes this CLI

Run it as:

```text
python -m scripts.run_s02_controlled_live <command> ...
```

Every command emits one compact, key-sorted JSON object. A successful command
returns exit code `0`. A blocked command returns a non-zero code and an explicit
error such as `SOURCE_PACKAGE_CHANGED`, `CHECKSUM_MISMATCH`,
`DURABLE_APPROVAL_MISSING`, or `QUEUE_NOT_EXCLUSIVE`.

## Source package manifest

The launcher never hardcodes a release fingerprint. The operator supplies a
strict JSON manifest with exactly these fields:

```json
{
  "dataset_id": "7707329152-debtam",
  "official_page": "https://www.nalog.gov.ru/opendata/7707329152-debtam/",
  "artifact_requested_url": "https://file.nalog.ru/opendata/7707329152-debtam/data-YYYYMMDD-structure-YYYYMMDD.zip",
  "artifact_final_url": "https://file.nalog.ru/opendata/7707329152-debtam/data-YYYYMMDD-structure-YYYYMMDD.zip",
  "artifact_filename": "data-YYYYMMDD-structure-YYYYMMDD.zip",
  "artifact_size": 1,
  "artifact_sha256": "64-lowercase-hex-characters",
  "xsd_requested_url": "https://file.nalog.ru/opendata/7707329152-debtam/structure-YYYYMMDD.xsd",
  "xsd_final_url": "https://file.nalog.ru/opendata/7707329152-debtam/structure-YYYYMMDD.xsd",
  "xsd_filename": "structure-YYYYMMDD.xsd",
  "xsd_size": 1,
  "xsd_sha256": "64-lowercase-hex-characters",
  "last_modified": "YYYY-MM-DD",
  "data_as_of": "YYYY-MM-DD",
  "source_as_of": "YYYY-MM-DDT00:00:00+00:00",
  "retrieved_at": "YYYY-MM-DDTHH:MM:SS+00:00",
  "official_actual_until": "YYYY-MM-DD",
  "structure_version": "YYYYMMDD",
  "real_xml_version": "4.01",
  "information_type": "ОТКРДАННЫЕ6",
  "validation_result": "PASS",
  "main_sha": "40-lowercase-hex-characters"
}
```

The size values above are placeholders, not accepted production values. The
manifest must contain the measured values for the reviewed package. Unknown or
missing fields, unsupported format metadata, mismatched filenames, invalid
official URLs, and non-`PASS` validation fail closed.

`artifact_requested_url` and `xsd_requested_url` describe the URLs exposed by
official discovery. The corresponding `*_final_url` fields retain redirect
evidence. Live rediscovery must still expose the requested URLs and the same
source date, data date, and official validity date.

The manifest `main_sha`, the operator's `--expected-main-sha`, and the runtime
Git revision must all be identical. In a packaged deployment without `.git`,
the image/build system may set `KONTRAGENT_RUNTIME_SHA` to the immutable full
commit SHA. If neither source can prove the revision, mutating commands stop
with `MAIN_SHA_NOT_VERIFIED`.

## Cohort format

Use UTF-8 JSON in either form:

```json
{"inns":["7707083893"]}
```

or:

```json
["7707083893"]
```

The launcher requires 1–100 unique, valid, 10-digit legal-entity INNs. It sorts
the normalized values and calculates `SHA-256((INN + "\n")...)`. Raw JSON
formatting and input order therefore do not change the cohort hash. Every INN
must already resolve to exactly one Master Company row with
`entity_type="legal"`; the launcher never creates Company records.

## Commands

### 1. Preflight (read-only)

```text
python -m scripts.run_s02_controlled_live preflight \
  --source-package manifest.json \
  --artifact data-release.zip \
  --xsd structure.xsd \
  --cohort cohort.json \
  --expected-main-sha <full-sha>
```

`--database-url` may override the canonical `DATABASE_URL`. The value is never
included in output. Preflight validates the manifest, local bytes, current UTC
freshness, live official rediscovery, cohort, Master Company membership,
baseline/source tables, worker publication state, generation metadata, safety
flags, and the current runnable queue. It rolls the session back and performs
no writes.

`official_actual_until` is inclusive. The day after it, the package is stale;
there is no override. If live discovery exposes another release, the command
returns `SOURCE_PACKAGE_CHANGED` even when the old package remains locally
available.

### 2. Approve (mutating)

Approve takes all preflight inputs and re-runs every gate before writing the
durable handler approval:

```text
python -m scripts.run_s02_controlled_live approve <preflight-inputs> \
  --approved-by '<operator identity>' \
  --approved-at 'YYYY-MM-DDTHH:MM:SS+00:00' \
  --confirm-controlled-live S02_CONTROLLED_LIVE_APPROVE
```

Approval commits separately and never enqueues a job. Generic confirmation
values such as `yes` or `true` are rejected.

### 3. Enqueue (mutating)

```text
python -m scripts.run_s02_controlled_live enqueue <preflight-inputs> \
  --artifact-store /bounded/operator/artifact-store \
  --retrieved-at 'YYYY-MM-DDTHH:MM:SS+00:00' \
  --timeout-seconds 3600 \
  --confirm-enqueue S02_CONTROLLED_LIVE_ENQUEUE
```

The reviewed default is 3600 seconds. Overrides are bounded to 600–7200
seconds. Enqueue re-runs preflight, requires the exact durable approval, passes
the manifest checksums, official discovery metadata, and normalized cohort to
`enqueue_fns_tax_debt_controlled_live_job`, and commits separately. It does not
execute the worker. The pipeline's checksum-based idempotency key means an
identical request returns the existing job; it never creates a second one.

If another runnable job would precede the new/reused job, enqueue rolls back
and returns `QUEUE_NOT_EXCLUSIVE`.

### 4. Run once (high risk)

```text
python -m scripts.run_s02_controlled_live run-once \
  --job-id <uuid> \
  --worker-id <stable-operator-worker-id> \
  --expected-main-sha <full-sha> \
  --confirm-run S02_CONTROLLED_LIVE_RUN
```

Before calling `WorkerExecutor.run_once`, the launcher proves that the job:

- exists in `queued` or `retry_scheduled` state;
- is `source_id=S02`;
- is `job_type=fns_tax_debt_controlled_live`;
- pins `fns-tax-debt-controlled-live-v1`;
- has the exact active durable approval; and
- is the first job under the worker's real `created_at, id` claim order.

The guard query and the executor's `SELECT ... FOR UPDATE` claim share one
`REPEATABLE READ` transaction and one database snapshot. A concurrent queue
insert/update is therefore either invisible to that claim or causes a
serialization failure; it cannot substitute another job between proof and
claim. The guard instant is also reused for the claim query, so a scheduled
retry becoming runnable between two wall-clock reads cannot change eligibility.
After execution, the returned `run_id` is resolved back to `WorkerRun` and must
reference the requested `job_id`. Any mismatch is a critical `JOB_MISMATCH`.
Exactly one call to `run_once` is made.

### 5. Status (read-only)

```text
python -m scripts.run_s02_controlled_live status [--job-id <uuid>]
```

Status uses accepted S02 monitoring and adds the selected job, its last run,
publication generations, active/rollback presence, checksum, freshness and
safe errors. Filesystem pointers and error text are not emitted: pointers and
messages are represented by SHA-256 identities.

### 6. Rollback (high risk)

```text
python -m scripts.run_s02_controlled_live rollback \
  --expected-generation <integer> \
  --expected-main-sha <full-sha> \
  --confirm-rollback S02_CONTROLLED_LIVE_ROLLBACK
```

The launcher first reads the current and rollback generation state. The current
worker generation must equal `--expected-generation` and a rollback pointer
must exist. It then calls only `rollback_fns_tax_debt_generation` and commits
the transaction. It does not implement pointer mutation itself. Output includes
safe before/after pointer identities, fact/query/normalized generations,
checksum, and freshness.

### 7. Evidence (read-only)

```text
python -m scripts.run_s02_controlled_live evidence --company-id <id>
```

Evidence calls `calculate_s02_vertical_slice_from_persisted` only. It performs
no provider requests and emits a bounded internal fact/Risk/Summary,
coverage/freshness, and limitations summary. Provenance paths, credentials and
private payloads are omitted. This is not a public release operation.

## Transactions and recovery

`approve`, `enqueue`, `run-once`, and `rollback` are independent transactions.
There is no transaction spanning the full Run A. Read-only commands always end
with rollback. If a command returns `BLOCKED`, resolve that named gate and start
the same phase again; do not bypass it with ad-hoc Python.

The launcher does not download the artifact, enable a scheduler, enable mass
ingestion, or run real controlled live as part of DEV-013 implementation or
tests. Automated integration tests use a synthetic ZIP/XSD and disposable
PostgreSQL only.
