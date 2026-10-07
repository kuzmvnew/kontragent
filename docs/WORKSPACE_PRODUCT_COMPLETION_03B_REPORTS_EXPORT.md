# Workspace Product Completion 03B — Reports and Export

## Product boundary

Reports are authenticated Workspace artifacts. A report belongs to exactly one
`workspace_id`; every history, detail, and export lookup includes that tenant
scope. Report routes are not mounted in the public application, and public
cards expose neither report identifiers nor report counts.

`CompanyAssessment` and `DealContext` in `app/contracts/report.py` remain
unchanged. `CompanyAssessment` is a validation container for one check and is
not Workspace persistence. `WorkspaceReport` is the separate historical
artifact introduced here. `DealContext` is accepted by the service as optional
validated user input, is labeled `USER_PROVIDED`, and explicitly does not alter
the current Risk or Summary result.

## Ownership and persistence

`workspace_reports` stores:

- a UUID report identifier and mandatory Workspace owner;
- the canonical global company reference with `ON DELETE SET NULL`;
- the generating customer user;
- `COMPANY_CHECK_V1` and `workspace-report-v1` version keys;
- customer-safe subject identity fields needed for historical rendering;
- Company View, Risk, and Summary references/versions;
- the immutable JSONB snapshot and its SHA-256 digest;
- generation and creation timestamps.

The report does not copy a Company entity. If the canonical Company row is
removed, only the optional foreign-key reference is cleared; the subject
identity and report snapshot remain available.

## Snapshot and immutability

Generation obtains the same customer-safe semantic projection used by the
authorized Company Card. The versioned envelope contains subject identity,
Company View metadata, Risk and Summary content, normalized semantic sections,
states, provenance, source data dates, freshness, limitations,
recommendations, and optional user-provided deal context. Provider payloads,
parser objects, RAW data, ingestion structures, and internal company database
IDs are excluded.

Opening or exporting a report reads only the stored snapshot. It never rebuilds
the current Company View. A later company refresh therefore leaves old reports
unchanged; a user must generate a new report to capture new product truth.

The database trigger rejects report updates. Its only exception is the
referential `company_id -> NULL` transition required by `ON DELETE SET NULL`;
all customer-visible report data remains byte-semantically immutable. There is
no edit or regenerate-in-place route.

## Canonical JSON and integrity

Snapshots are normalized to supported JSON values and serialized as UTF-8 with
sorted keys and compact stable separators. `snapshot_sha256` is the SHA-256 of
that canonical byte sequence. Detail and export reads recompute the digest and
fail closed with `snapshot_integrity_failed` when it does not match.

The JSON download is the deterministic stored semantic envelope, served as
`application/json` with a safe attachment filename:

`next-company-report-{inn}-{yyyy-mm-dd}-{report_id}.json`

## CSV export

CSV rows flatten stored semantic facts into these columns:

`report_id`, `generated_at`, `inn`, `company_name`, `section_key`,
`section_title`, `field_key`, `label`, `state`, `value`, `period`, `source`,
`source_data_date`, `freshness`, `limitations`.

Mappings and arrays use the same sorted compact JSON serialization; Python
`repr()` is never emitted. One canonical field helper applies serialization and
the export-safety policy to every scalar and structured field. It preserves
ordinary customer text, whitespace, and Unicode format characters. When the
first meaningful character after any leading Unicode whitespace or `Cf` format
characters is `=`, `+`, `-`, or `@`, the original text receives an apostrophe at
the very beginning so spreadsheet applications treat it as text; the accepted
leading characters themselves are never stripped.

NUL fails closed with the deterministic `report_export_invalid_value` error.
Other C0/C1 controls also fail closed, except tab, LF, and CR, which CSV quoting
can represent and which still participate in formula-prefix detection. Unicode
surrogate code points are rejected; Unicode format characters such as U+FEFF
and zero-width format values are retained but ignored when locating a formula
prefix. Export is built fully in memory, so an invalid value cannot produce a
partial download. Both HTML and API CSV routes return the same semantic error
policy without exposing the rejected content or a stack trace.

Before structured dict, list, or tuple values reach the first UTF-8 encoding
operation, the CSV boundary recursively validates every nested string as well
as each dictionary key's normalized string representation. Nested NUL,
forbidden C0/C1 controls, and surrogate code points therefore fail with the
same `report_export_invalid_value` contract as scalar fields. Tab, LF, CR, and
Unicode format characters remain allowed inside structured strings and retain
canonical JSON escaping. Nested strings are never rewritten for formula
safety: prefix neutralization applies only to the final flattened CSV cell.
Unexpected Unicode serialization failures are also translated to the same safe
export error as defense in depth.

CSV uses RFC-compatible quoting, CRLF records, and UTF-8 with BOM
(`utf-8-sig`) for reliable Russian-language spreadsheet import. Filenames use
the same safe pattern with a `.csv` suffix. These transformations affect only
the CSV representation: the stored snapshot, its SHA-256, and JSON export are
unchanged.

## Permissions, entitlement, and quota

The explicit capabilities are `report.view`, `report.generate`, and
`report.export`. OWNER, ADMIN, and MEMBER receive all three in the current P0
Workspace role contract. Authorization is server-side for HTML and API routes.

`reports.enabled` gates only new generation. Historical reports remain
viewable and exportable to members who retain their corresponding permissions
and core Workspace access after the entitlement is disabled. Bootstrap creates
the entitlement enabled, and the migration backfills it for existing
Workspaces while adding capabilities to existing system roles.

`WorkspaceEntitlement.limit_value` is the optional maximum stored report count.
The default is unlimited. Generation locks the canonical entitlement row,
counts reports inside the transaction, and returns `quota_exceeded` when the
limit is reached, which serializes concurrent generation attempts per
Workspace.

## Transaction and audit

The service adds the report row and `report.generate` success audit event in one
caller-owned database transaction. A failed generation is rolled back and has
no success audit. Quota denial records a bounded company reference and no
report content. Audit targets never contain snapshots or provider payloads.

## Routes

HTML:

- `POST /app/companies/{inn}/reports` — CSRF-protected generation;
- `GET /app/reports` — bounded history (default 50, hard maximum 100);
- `GET /app/reports/{report_id}` — stored snapshot detail;
- `GET /app/reports/{report_id}/export.json`;
- `GET /app/reports/{report_id}/export.csv`.

API:

- `POST /app/api/companies/{inn}/reports` — session CSRF header required;
- `GET /app/api/reports`;
- `GET /app/api/reports/{report_id}`;
- `GET /app/api/reports/{report_id}/export.json`;
- `GET /app/api/reports/{report_id}/export.csv`.

HTML and API call the same report service and use the same snapshot, permission,
tenant, integrity, and export logic. A report UUID from another Workspace
returns the safe `report_not_found` result.

## Known gaps

- PDF export and branded print polish are not included; the HTML detail has a
  lightweight print stylesheet.
- Bulk Check remains a separate 03C architecture task.
- Users/Roles UI is not implemented.
- Settings/Usage UI is not implemented.
- Demo Workspace is not implemented.
