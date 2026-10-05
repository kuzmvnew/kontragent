# Monitoring P0 vertical slice 01

Task: `MAC-OFFLINE-WAVE-C-MONITORING-P0-VERTICAL-SLICE-01`
Base: `091dc157d2707ced437ef70096c9238c5ece25a4`
Branch: `mac/monitoring-p0-foundation-01`
Production mutation: **NO**

## Current state audit

| Area | Fact at base | Tested before this slice | Gap closed here |
| --- | --- | --- | --- |
| Saved Company | `SavedCompany` is unique by Workspace and global Company | Workspace P0 PostgreSQL tests | Subscription requires it |
| Authorization | Active membership, role capability, and entitlement are checked by `authorize` | Workspace P0 PostgreSQL and browser tests | Monitoring writes use `monitoring.manage`; feed reads and writes are Workspace scoped |
| Entitlement | `monitoring.enabled` defaults to false | Workspace P0 bootstrap tests | Remains false by default; no implicit activation |
| Semantic facts | `CompanySemanticFact` has stable semantic coordinates, state, rights, and selected evidence | Company View tests | Immutable Monitoring snapshots use these facts |
| Company View | `CompanyViewModelV1` is the semantic contract | Company View tests | Snapshot uses its persisted semantic facts and a separate Monitoring fingerprint; actual View revision is null unless it becomes available from persisted state |
| Other domain objects | `CompanyLegalEvent`, `SourceChangeSummary`, Risk v3 and Summary v3 already exist | Existing domain tests | No duplicate source or risk object introduced |
| Workspace UI | Monitoring entry page reported `NOT_IMPLEMENTED` | Workspace P0 browser tests | Real state, actions, and in-app feed |
| Migration | Single head `d3e5f7a9b1c4` | Alembic heads | Single successor `e4f6a8b0c2d3` |

## Domain and data model

`MonitoringSubscription` is Workspace owned and unique for one Workspace plus one global Company. Statuses are `ACTIVE` and `PAUSED`. It carries actor, start, pause, last check, and baseline snapshot cursor. The stronger all-status uniqueness also prevents multiple active subscriptions. An active subscription blocks unsaving the company until it is paused.

`CompanyMonitoringSnapshot` is a new immutable company-level row for each baseline and each scan with at least one active entitled subscription. It contains only current accepted semantic facts with `PUBLIC` or `AUTHENTICATED_ONLY` rights. Each fact contains stable coordinate, fact reference, state, selected normalized value, bounded evidence reference, source code/class, source data date, and rights. It excludes RAW, provider JSON, source URLs, credentials, parser objects, alternative evidence payloads, internal facts, retrieval timestamps, and ingestion identifiers. It records latest existing Risk v3 and Summary v3 references without calculating a new risk assessment. The `company_view_revision` field is nullable because the actual materialized View revision is not stored with `CompanySemanticFact`; the SHA-256 Monitoring fingerprint is distinct.

`MonitoringEvent` is canonical company-level change history. It stores origin, generic or semantic event type, change kind, coordinate, old/new JSON values and states, severity and policy version, detection time, optional occurrence/source date, semantic evidence refs, visibility flag, and deterministic dedupe key. It is separate from `CompanyLegalEvent`, which remains a source/business fact. `WorkspaceFeedEntry` is the tenant delivery pointer with read time. The database enforces unique subscription/event delivery and a composite foreign key that requires its Workspace to match the subscription's Workspace.

## Subscription and feed lifecycle

Enabling requires an active user and Workspace, `monitoring.manage`, `monitoring.enabled`, and a saved company in that Workspace. Enabling captures an initial baseline and creates zero events. Repeated enabling is idempotent. Pausing preserves history and stops scanning and fanout for that subscription. Resuming captures a new baseline, so changes during the pause are not replayed. The feed is ordered by event detection time, newest first. Marking an entry read checks its Workspace scope. Subscription mutations and read receipts write a safe audit action in the same transaction.

Disabling the entitlement blocks scans and feed access. It does not delete subscriptions or history.

## Detection and event contract

The system scan in `monitor_company_once` reads only `CompanySemanticFact` and existing Risk/Summary references. It compares facts by `section_key + field_key + period_identity + item_identity`, not database row IDs or ingestion timestamps. Canonical JSON serialization makes key order and fact order irrelevant. The detector supports `FACT_ADDED`, `FACT_CHANGED`, `FACT_REMOVED`, and `STATE_CHANGED`.

`SOURCE_CHANGE` is the client-visible path. `COVERAGE_CHANGE` is persisted but suppressed from the Workspace feed. `RULESET_CHANGE` and `DEAL_CONTEXT_CHANGE` are reserved origins in the schema; no P0 producer emits them. Missing facts are treated as coverage loss, never as a proved resolution. `SOURCE_UNAVAILABLE`, timeout, parsing error, stale, not-checked, unknown, partial, and conflicting-evidence transitions are coverage changes. `FOUND → NOT_FOUND` remains a semantic state change; `FOUND → SOURCE_UNAVAILABLE` cannot become a false “resolved” alert. The detector ignores `retrieved_at`, `checked_at`, internal DB timestamps, job IDs, checksums, parser versions, and alternative evidence ordering.

The dedupe key hashes company, stable coordinate, origin/type/kind, old/new semantic states and values, and old/new source data dates. The same semantic transition is one canonical event across Workspaces, with one feed entry per active entitled subscription. Event and delivery uniqueness are both enforced in PostgreSQL. The event stores the selected semantic source code and evidence refs, with no source payload.

## Severity

Policy `monitoring-severity-p0-v1` is independent of Numeric NEXT Index and Risk score:

| Level | Registry families |
| --- | --- |
| HIGH | company status, bankruptcy, restrictions |
| MEDIUM | management, founders, tax debt, enforcement, licenses, courts |
| LOW | address, inspections, finance |
| INFO | generic changes, coverage changes, known resolved states |

The registry falls back to `GENERIC_FACT_CHANGED` for new semantic fields. A new source can map to existing semantic facts without changing the detector. No `CRITICAL` level is defined.

## API and UI

Authenticated routes:

- `GET /app/api/companies/{inn}/monitoring`
- `POST /app/api/companies/{inn}/monitoring/enable|pause|resume`
- `GET /app/api/monitoring`
- `POST /app/api/monitoring/feed/{entry_id}/read`

HTML pages are `/app/companies/{inn}/monitoring` and `/app/monitoring`. HTML and API writes use the existing session and CSRF checks. Company state displays `ACTIVE`, `PAUSED`, or `NOT_ACTIVE`, start and last-check times, and available actions. The feed displays company, INN, event title, old/new values, severity, detection time, semantic source/evidence labels, read state, and a link to the authorized card. It has an explicit empty state. It makes no real-time or daily schedule claim.

## Scheduler boundary and local scan

No production scheduler, worker schedule, notification channel, or public detector endpoint is installed. A controlled local system scan is available through `scripts/run_monitoring_scan.py --inn <inn>`; it refuses non-local Workspace environments and does not create or authorize subscriptions. The domain service can be called directly in disposable integration tests.

## Test evidence and limitations

- Fresh upgrade to the new head passed on a disposable local PostgreSQL database.
- Downgrade to `d3e5f7a9b1c4` and re-upgrade to head passed.
- Schema completeness: compatible, no errors.
- Monitoring P0 PostgreSQL/API tests: 9 passed, zero skipped.
- Workspace, browser, Company View, S02, and schema focused regression: 194 passed, 3 unrelated skips.
- Real Chromium Monitoring flow: login, save, enable, semantic change, controlled scan, feed, card, pause passed. A second browser test proved separate entries and cross-Workspace read denial.
- Final full repository suite: 1,830 passed, 20 skipped, zero failures. The Mac's disk was below the existing Firmoteka safety threshold, so this local run set `FACTORY_MIN_DISK_FREE_PERCENT=1`; the unmodified threshold caused three unrelated Firmoteka scale tests to pause intake.

Known P0 limits: there is no production scan cadence or delivery outside the in-app feed; source dates are not an authoritative event occurrence timestamp; absent facts do not produce client-visible resolved alerts; pause/re-enable does not replay historical events; a recurrence of the exact same transition with the same source data dates shares its dedupe identity. No production Monitoring acceptance or deployment is claimed.
