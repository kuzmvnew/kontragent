# Workspace Product Completion 03C — Bulk Check

## Product flow and boundary

Bulk Check is an authenticated, tenant-owned Workspace feature:

`Workspace → Массовая проверка → CSV upload → validation/deduplication → durable job → chunk processing → results → filter/card/export`.

It analyzes data already accepted into the canonical `Company` master and the
customer-safe frozen `PublicProjection` release. It does not call a provider,
discover or download a source, enqueue `WorkerJob`, run ingestion, crawl
Firmoteka, save a company, enable monitoring, or generate a report. No Bulk
route is mounted in the public application.

## Input contract

- CSV only, decoded strictly as UTF-8; UTF-8 BOM is accepted.
- Maximum upload size is 2 MiB. Routes read at most `limit + 1` bytes and the
  service independently enforces the limit.
- Maximum 5000 nonblank data rows.
- A trimmed, case-insensitive `inn` or `инн` header is required exactly once.
- Comma, semicolon, and tab are the only supported delimiters. Ambiguous
  headers are rejected rather than guessed.
- CSV parsing is strict. Invalid UTF-8, NUL, malformed quotes, mismatched rows,
  duplicate headers, and missing INN headers are file-level failures and create
  no partial job.
- Physical blank lines are ignored. A present row with an empty INN value is
  retained as `INVALID_INN`.
- P0 accepts only checksum-valid 10-digit legal-entity INNs using the same
  `valid_legal_inn` contract as public cards. Twelve-digit IP INNs receive the
  explicit `ip_inn_unsupported` row outcome.
- Leading/trailing whitespace is removed only for validation and matching;
  `raw_inn` remains available for row accounting and safe export.

The original upload is not stored. The database receives a SHA-256 of the
input, a path-independent/control-normalized filename capped at 240 characters,
and normalized row records.

## Deduplication and accounting

Rows retain the CSV record number and are always displayed/exported in
`row_number ASC` order. The first valid occurrence of an INN becomes
`PENDING`. Each later occurrence is `DUPLICATE` with `duplicate_of_row` pointing
to the first occurrence. Invalid and duplicate rows remain visible but are not
processed again.

## Persistence and tenant isolation

`WorkspaceBulkJob` stores the versioned job state, safe file metadata, pinned
release coordinates, counters, lifecycle timestamps, and bounded safe error
metadata. `WorkspaceBulkItem` stores each uploaded row and its terminal result.
The item uses a composite `(workspace_id, job_id)` foreign key to the matching
job scope. Every service query additionally scopes by the active Workspace, so
a UUID from another tenant returns `bulk_job_not_found` for view, processing,
cancel, resume, retry, and export.

`Company.id` remains the only canonical company identity. A Bulk item may
reference it with `ON DELETE SET NULL`; there is no Bulk-specific company
master.

## State models

Job schema: `workspace-bulk-check-v1`.

Job states:

- `READY` — validated and awaiting a processing chunk;
- `RUNNING` — at least one chunk has started and pending rows remain;
- `COMPLETED` — all processable rows reached ordinary terminal outcomes;
- `COMPLETED_WITH_ERRORS` — terminal with one or more `PROCESSING_ERROR` rows;
- `CANCELLED` — user cancellation converted remaining pending rows;
- `FAILED` — reserved for unrecoverable job-level structural failure.

Item states:

- input: `PENDING`, `INVALID_INN`, `DUPLICATE`;
- customer/product: `READY`, `NOT_RESOLVED`, `NOT_READY`;
- operational: `PROCESSING_ERROR`, `CANCELLED`.

`NOT_RESOLVED` means only that the company was not found in the current
next.company canonical dataset. It never claims that a legal entity does not
exist. `NOT_READY` means the canonical company exists but the pinned public
release has no customer-safe projection. Master/RAW fields are not substituted.

Counters are recalculated from item states after every chunk or lifecycle
mutation. Invalid, duplicate, not-resolved, and not-ready outcomes do not turn
the job into an infrastructure failure.

## Public release pinning and processing

The first processing call reads the active public release and requires a
positive declared record count equal to the actual projection count. It then
persists `public_release_id` and `public_schema_version`. Every later chunk,
resume, or retry calls `PublicRepository.get_companies(...,
release_id=pinned_id)`; the repository never falls back to the current active
release.

One bounded chunk (default 75, hard maximum 100) performs:

1. a tenant-scoped `FOR UPDATE` lock on the job;
2. one query for pending items;
3. one query for canonical Companies by exact INN;
4. one read-only public query for exact INNs under the pinned release;
5. terminal row snapshot writes and a grouped counter reconciliation.

The job lock serializes concurrent process-next calls, preventing duplicate
row processing and result writes. PostgreSQL is the queue/state owner; no
FastAPI background task, thread queue, or browser memory is authoritative.

## Result snapshot

Successful items store `workspace-bulk-result-v1` with release ID, checked and
result dates, minimized company identity, public Risk state/status/title and
explanation, Summary conclusion, existing Company View section-state keys, and
the limitation count. The SHA-256 covers canonical JSON bytes. Internal IDs,
RAW data, provider payloads, parser objects, and operational references are
excluded. Opening an individual result links to `/app/companies/{inn}`.

The stored payload is historical. Later releases do not alter an old job or
its exports.

## Cancel, resume, retry, and idempotency

Cancel locks the job and changes only remaining `PENDING` items to `CANCELLED`;
already terminal rows remain. Resume changes those cancelled items back to
`PENDING` on the same job and pinned release. Retry changes only
`PROCESSING_ERROR` rows back to `PENDING`; it never retries invalid, duplicate,
not-resolved, or not-ready product outcomes. Repeated lifecycle calls are safe
no-ops or deterministic conflicts and never create new items.

## Permissions, entitlement, CSRF, and audit

OWNER, ADMIN, and MEMBER receive `bulk.view`, `bulk.create`, `bulk.manage`, and
`bulk.export` in P0. HTML and API mutations use the existing session CSRF
contract. `bulk_check.enabled` gates creation and management; its
`limit_value` is the maximum unique valid INNs per job, default 1000. The 5000
row technical cap is independent. Migration backfill enables existing
Workspaces and grants all four capabilities to existing roles.

Audit records cover `bulk.create`, terminal `bulk.process`, `bulk.cancel`,
`bulk.resume`, `bulk.retry`, and each successful CSV/JSON `bulk.export`. No
per-company audit spam or uploaded content is stored in audit targets.

## UI, API, filtering, and pagination

`/app/bulk` provides upload and bounded job history. `/app/bulk/{job_id}` shows
durable progress and paginated results (default 50, hard maximum 100), with
server-side exact status and bounded INN search filters. A no-JavaScript form
processes one chunk. Minimal page JavaScript may repeatedly call the same
idempotent process endpoint; reload or browser close cannot corrupt the job.

Equivalent API routes live under `/app/api/bulk-jobs`. HTML and API share the
same services and authorization rules.

## Exports

CSV and JSON read stored job/item state only. JSON contains customer-safe job
metadata and result rows, but no workspace, company, or item database IDs. CSV
has a stable column set and UTF-8 BOM/CRLF output. It reuses the accepted 03B
CSV cell contract: recursive structured validation, fail-closed handling for
NUL/C0/C1/surrogates, and spreadsheet formula neutralization after leading
Unicode whitespace or `Cf` characters. An invalid value yields
`bulk_export_invalid_value`; no partial file is returned.

## Known gaps

- P0 processing is driven by explicit browser/API chunks; a production
  background scheduler is future work.
- XLSX input, very large asynchronous imports, and tariff-specific limits above
  the current bounded contract are not included.
- Bulk save, monitoring, and report actions are deliberately not included.
