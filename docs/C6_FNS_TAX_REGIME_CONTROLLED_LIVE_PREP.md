# C6 FNS Tax Regime — controlled-live preparation

Task: `MAC-OFFLINE-WAVE-D-C6-FNS-TAX-REGIME-CONTROLLED-LIVE-PREP-01`

Base: `2c92f86762073174fd8a74c9d7517e7776432646`

Maximum status: `READY_FOR_HOME_CONTROLLED_LIVE`. This document does not
authorize production activation, a public release, or a multi-source run. The
exact merge-candidate SHA is the head of the dedicated PR and must be copied
from the QA handoff; HOME must fail the preflight if its checked-out
`git rev-parse HEAD` is not that exact value.

## Current-state audit

### FACT

- `fns_tax_regime` is one operational source family. Its mandatory member
  datasets are `fns_snr` (legal entities) and `fns_snrip` (individual
  entrepreneurs).
- The durable handler is
  `fns_tax_regime@tax-regime-family-official-v1`. One Worker job contains both
  release members. The scheduler route is the real
  `schedule_fns_tax_regime_check` function; neither a fixture nor a generic
  placeholder is used.
- Worker staging is checksum-addressed and write-once. The accepted family
  pointer references a bundle descriptor whose two normalized members and
  checksums are pinned. Same-release replay reads that exact accepted pointer
  and does not re-download the source.
- Publication exact-matches INN, validates entity applicability, and changes
  both member projections in the Worker's publication transaction. The unique
  key is `(company_id, dataset_id, data_date)`.
- Product projection uses the tax-regime service for the current card and also
  materializes `tax.regime` into Company View as a public, provenance-backed
  semantic fact. Monitoring consumes persisted semantic facts, so a regime
  change follows the generic semantic diff and `GENERIC_FACT_CHANGED` path.

### TESTED

- Legal/IP parser fields, flags/codes, missing identity, invalid INN/OGRNIP,
  malformed/schema-invalid inputs, source/XSD path pins, member completeness,
  mixed-date rejection, duplicate-INN union and conflicts, exact entity
  applicability, atomic PostgreSQL publication, late-Master replay and second
  replay idempotency, freshness, handler approval, scheduler binding,
  Company View materialization, generic Monitoring mapping, operator
  preflight, and disk gates are deterministic tests.
- The official probe is intentionally separate from the ordinary suite.
  External FNS availability is not a unit-test dependency.

### SOURCE VERIFIED — 2026-10-06

Both official passports returned HTTP content and independently identified
their pinned datasets:

| Member | Passport identifier | Current ZIP | Current XSD | Source data date | Passport changed | Actual through |
| --- | --- | --- | --- | --- | --- | --- |
| Legal | `7707329152-snr` | `data-20260925-structure-20230425.zip` | `structure-20230425.xsd` | 2026-09-01 | 2026-09-25 | 2026-10-25 |
| IP | `7707329152-snrip` | `data-20260925-structure-20241025.zip` | `structure-20241025.xsd` | 2026-09-01 | 2026-09-25 | 2026-10-25 |

The hardened repository discovery code accepted the pair as bundle identity
`9d92384f7470b2f6311bbd3ae9dc043be0c6e89e3544e8ce1fc502f3833a5fad`
(Legal member `d0fa3e546fee26f0cb658b11503d379d341a69923fc0ea1bdda008acddb9513c`,
IP member `e306cf86d3d8daad5ed55c1cde20efd15dbcbbcf67109377bb0d0d43bedd9b15`).
HOME must rediscover rather than treating these September identities as
permanent configuration.

Read-only HEAD evidence reported 62,028,770 bytes for Legal and 293,908,849
bytes for IP. Legal advertised SHA-256
`54516b37031426af7413f7e35ea57d9554e9ec578478bbb2612abe2e3dcac1b4`.
The IP response did not advertise `x-amz-meta-sha256`; the worker therefore
computes and pins its own SHA-256 after the verified download. The XSD files
were 13,116 and 11,847 bytes, with SHA-256 respectively
`6d7631b77e828c05ff3f1ef43915084b195092dd7fa7f44a5d325f7fefb99332`
and `11ab9c913237851bd4dbc8d52e252513041869e5dc9e954a1337a81b6f461e8d`.

### GAP closed by this preparation

- Passport discovery now requires exact `dc:identifier`, `dc:modified`,
  current ZIP/XSD links under the pinned dataset path, and equal structure
  version tokens in the ZIP and XSD names.
- A family bundle with different member `source_data_date` values is rejected,
  instead of reporting the older value as if the mixed bundle were accepted.
- Parser-level document identity and IP OGRNIP validation now fail safely even
  before XSD enforcement. A future schema-valid but unknown regime value is
  retained in normalized QA evidence and stops C6 at threshold zero.
- Exact-INN publication rejects a conflicting Company entity type.
- QA coverage includes source/parsed/invalid/unknown/duplicate/matched/
  unmatched/published/replayed/member counts and the family bundle identity.
- A dedicated read-only HOME preflight and separately confirmed, source-scoped
  enqueue command now exist. They never enable the production scheduler.
- Company View now receives a canonical `tax.regime` fact; this makes changes
  structurally monitorable without a source-specific Monitoring engine.

### Correction 1 — QA findings F-C6-134-01/02/03

- The tax-regime service and Company View now share one family evaluator. It
  requires the family and both mandatory members to be current and release
  compatible. A failed sibling degrades either entity type.
- Snapshot absence no longer proves a negative. Terminal
  `CompanySourceCoverage` must identify the exact current generation, replay
  pointer, checksum, release identity and source data date.
- Discovery and staging use one canonical official URL validator before a
  download or RAW directory is created.

### Correction 2 — QA finding F-C6-134-04

- The HOME preflight now reports `release_mode`, persisted
  `family_readiness_state`/`family_readiness_reason`, current publication
  identity and discovered bundle identity. For `SAME_RELEASE`, it reuses the
  product's canonical three-component family evaluator and blocks enqueue
  unless the persisted family is current and matches the accepted bundle.
- `INITIAL_RELEASE` has no accepted identity to compare. `NEW_RELEASE` reports
  old-family readiness diagnostically but does not let an expired previous
  generation deadlock ingestion of a valid new bundle. Both modes still require
  valid official member links, source pins, handler approval, registration and
  disk gates.

### Correction 4 — QA findings F-C6-134-06/07/08

- Publication admission first classifies the durable C6 lifecycle as
  `NO_PUBLICATION`, `ACCEPTED_VALID`, or `CORRUPT`. The worker creates a
  `WorkerPublicationState` row only after accepted staging; therefore a row
  with missing/incomplete validation is corrupt, never pristine bootstrap.
- Genuine `INITIAL_RELEASE` requires **no** publication-state row **and no**
  durable prior-publication evidence in the family/member datasets or C6
  snapshots. A deleted state row beside release coverage, publication dates,
  record counts, or facts is `CORRUPT` (`lost_accepted_publication_state`).
- Accepted proof requires strict positive integer generation (booleans and
  coercions do not count), strict nonnegative fencing token, canonical
  64-character lowercase SHA-256 checksum and family/member identities,
  recomputed family composition, valid date/pointer, and coherent family and
  child coverage. A malformed old accepted graph cannot become `NEW_RELEASE`.
- `SAME_RELEASE` additionally hashes the accepted descriptor bytes and matches
  that digest to the persisted checksum. It validates the descriptor's exact
  family/member structure, release anchors, normalized pointers, and actual
  normalized file hashes. Missing, malformed, substituted, or modified RAW is
  a STOP condition; preflight never repairs it.
- Read-only preflight exposes the publication lifecycle, proof reasons,
  canonical checksum and descriptor verdicts, and a canonical SHA-256
  `admission_fingerprint` over the safety-critical observed evidence. Confirmed
  enqueue locks the family, both children, publication state, and handler in
  one transaction, recomputes the full admission report against the same
  discovered bundle, rechecks live resource/disk gates, and compares the
  fingerprint. A changed generation, checksum, mode, coverage, source URL,
  handler, descriptor, or readiness yields `controlled_live_preflight_stale`
  and zero jobs. Re-run preflight after investigating; do not override.

### NOT VERIFIED by Mac preparation

- Full artifact download, XML validation, normalization and PostgreSQL
  publication of the current 2026-09-01 release have not been executed.
- Current live record counts, match rates, duplicate counts, unknown-code
  counts, normalized checksums and the IP artifact checksum are intentionally
  unknown until the HOME controlled-live run.
- Commercial-use/legal publication approval is a separate gate. Official open
  data status is not a commercial-use acceptance decision.

## Source and data contract

The family is complete only when both `legal` and `ip` are present. Their
`source_data_date` must be identical. The conservative family
`actual_until` is the earlier member boundary; if either boundary is missing,
family freshness is unavailable. One successful member never advances family
publication or family currentness.

Legal SNR requires a 10-digit `ИННЮЛ`, `ИдДок`, valid `ДатаДок` and
`ДатаСост`, and the required `0/1` flags `ПризнУСН`, `ПризнАУСН`,
`ПризнЕСХН`, `ПризнСРП`. IP SNRIP requires a 12-digit `ИННФЛ`, 15-digit
`ОГРНИП`, document identity/dates, one or more `СведСНР`, and maps only `1`
through `5` to USN, AUSN, ESHN, PSN and NPD. Unknown values are never coerced
to false and never assigned an invented meaning.

Duplicate INNs are sorted and coalesced on disk. Regime codes are unioned;
source documents are deterministically preserved. Mixed dates, entity/member
applicability changes, or conflicting OGRNIP values fail the release.

## RAW, XSD and replay contract

For each member the worker writes:

```text
$FNS_RAW_ROOT/fns_tax_regime/<artifact-sha256>/<official-zip-name>
$FNS_RAW_ROOT/fns_tax_regime/<artifact-sha256>/<official-xsd-name>
$FNS_RAW_ROOT/fns_tax_regime/<artifact-sha256>/manifest.json
```

The family descriptor is:

```text
$FNS_RAW_ROOT/fns_tax_regime/bundles/<bundle-identity>/normalized-bundle.json
```

Files are write-once. Existing content must match its checksum and immutable
manifest identity. A new release creates a new checksum generation. Replay
requires the current `WorkerPublicationState.active_pointer`, its accepted
checksum and its exact bundle identity. If Company A existed on first
publication and Company B was added later, replay adds B, leaves A unique, and
the second replay writes zero rows.

The complete XML member is validated against its downloaded, path- and
version-pinned official XSD before parser output is admitted. Malformed ZIP,
CRC failure, malformed XML, wrong schema, missing required identity, bad date,
bad identifier, empty release or mixed data dates fail closed.

## Publication, status and product semantics

Family and member status record `enabled`, `operational_status`,
`last_success_at`, `last_data_date`, `source_as_of`, `retrieved_at`,
`published_at`, `record_count`, coverage, `official_actual_until` and
`next_expected_update_at`. Member schedules remain disabled because the family
owns both projections. `CURRENT` is allowed only through the inclusive
official `actual_until`; the next day is `STALE`. A stale/unverified mandatory
member makes the family non-current.

Product state is explicit:

- `FOUND`: all three family components are current and an applicable current
  member snapshot positively matches the Company;
- `NOT_FOUND`: all three components are current, no positive snapshot exists,
  and successful terminal Company coverage proves absence in the accepted
  frozen release;
- `NOT_CHECKED`: family readiness or current Company coverage is unverified,
  pending, running or tied to an older generation;
- `SOURCE_UNAVAILABLE`: operational/source failure prevents a conclusion;
- `PARSING_ERROR`: accepted semantic projection sees a parser/schema failure;
- `STALE_DATA`: the official/currentness boundary expired;
- `UNKNOWN`: reserved for an unresolved semantic condition, never converted to
  a clean negative.

The accepted RAW can contain a Company absent from Master at initial
publication. Before replay, its missing local snapshot is `NOT_CHECKED`. The
existing Company Enrichment local bulk replay checks the accepted RAW; if the
INN is present, it creates a snapshot and terminal `FOUND` coverage. A second
replay writes zero new facts. A genuinely absent INN becomes `NOT_FOUND` only
after successful terminal coverage. Advancing the accepted generation,
pointer, checksum or release identity invalidates historical negatives.
The compatibility check/Card retains `result=unavailable` for incomplete
checks and carries `semantic_state` for `NOT_CHECKED`, `STALE_DATA`,
`PARSING_ERROR` and `SOURCE_UNAVAILABLE`.

The official ZIP/XSD authority must be HTTPS on the FNS allowlist, without
credentials, port, query or fragment. Exactly three nonempty path segments
follow the root: `opendata/{pinned-dataset}/{official-filename}`. Literal,
encoded or nested traversal, encoded separators, backslashes, controls,
duplicate slashes and malformed percent escapes stop discovery or staging
before artifact download. A transient passport HTTP 403 fails preflight as
source/network unavailable; source identity checks remain mandatory.

The card may state the applicable official regimes. It must not infer low tax,
tax avoidance or tax risk. Risk v3 and Summary v3 do not currently consume Tax
Regime as a rule: `NOT INTEGRATED`. Company View exposes the factual semantic
value and provenance only. Monitoring treats its change as a generic semantic
fact change.

Product provenance includes FNS, member dataset, source data date, retrieved
and published timestamps, family/member release identity and artifact/XSD
checksums. RAW paths and provider objects are not exposed to the frontend.

## Source QA metrics

Controlled-live acceptance must capture, per member and family:

- source records and parsed records;
- invalid/quarantined records (accepted publication requires zero);
- unknown code count and values (safe threshold: zero);
- duplicate INN rows coalesced and normalized unique INNs;
- matched and unmatched companies;
- published snapshots and replayed facts;
- Legal/IP member counts;
- member release identities and family bundle identity.

`coverage` is a measured count set, not a “100% coverage” claim.

## HOME preflight — read-only

Required environment:

```text
DATABASE_URL=<HOME PostgreSQL URL>
FNS_RAW_ROOT=<dedicated immutable RAW filesystem path>
```

Do not set `FACTORY_MIN_DISK_FREE_PERCENT` below its production default of
10%. The preflight also requires at least 5 GiB free after its conservative
peak estimate. The current compressed ZIP+XSD total is 355,962,582 bytes; the
current estimate is 1,423,850,328 bytes (4x) for downloads/RAW, normalized
JSONL, SQLite coalescing and temporary output. This estimate is recalculated
from live HEAD responses every run.

Before the command, verify the QA-approved PR head:

```bash
test "$(git rev-parse HEAD)" = "$EXPECTED_C6_SHA"
```

Then run only:

```bash
PYTHONPATH=. uv run python -m scripts.run_fns_tax_regime_controlled_live \
  preflight --raw-root "$FNS_RAW_ROOT"
```

The command dynamically discovers both official passports, checks identity,
bundle compatibility, handler approval, the three dataset registrations,
whether the bundle is new, live content lengths and disk gates. It also
classifies the release mode:

- `INITIAL_RELEASE`: no state row and no persisted C6 publication evidence;
  bootstrap dataset currentness is diagnostic, not a prerequisite.
- `NEW_RELEASE`: a **coherent** accepted old proof has a different identity
  from the discovered bundle; old family readiness is diagnostic, but old
  publication integrity is mandatory.
- `SAME_RELEASE`: accepted family identity matches fresh discovery, but that
  alone is insufficient. The fresh official bundle is the authoritative
  identity/date anchor. The preflight checks the accepted Worker publication,
  family top-level coverage, the exact `legal`/`ip` family member map, and the
  `fns_snr`/`fns_snrip` child coverage against the corresponding fresh member
  identities and dates. Persisted values cannot self-attest: even coordinated
  legal and child, IP and child, or both-member substitutions are STOP conditions.
  Persisted publication member identities and dates, when present, must also
  agree with the fresh bundle; checksums do not replace release identities.
  Missing/malformed/extra members, swapped identities, stale dates, invalid
  publication generation/pointer/checksum, descriptor bytes/content, or
  unavailable/stale/parse-failed siblings block confirmed enqueue with zero
  Worker jobs. Enqueue locks and rechecks the entire admission proof, not
  merely release mode. The accepted replay pointer must address this bundle's
  canonical normalized descriptor under the selected RAW root.

`CORRUPT_PUBLICATION` is always `BLOCKED`: it is neither initial nor a new
release. Do not delete or manually rewrite state/RAW to force another mode;
investigate and repair under a separate recovery procedure. A stale admission
fingerprint means state changed between preflight and enqueue; stop and rerun
preflight, reviewing the difference before another confirmation. The final
locked check includes current disk safety (at least 10% and 5 GiB after the
conservative peak estimate), handler approval, and exact source registrations.

All modes retain source identity, exact URL/path, handler, registration and
resource gates. Direct invalid bundle inputs are rejected before HEAD probes.
The preflight rolls back its DB session and creates no job or file.

Expected preflight result: `READY_FOR_CONTROLLED_LIVE`, exact source/handler,
both member codes, no blockers, and `release_mode=INITIAL_RELEASE` with
`new_release_exists=true` for a first publication. On reruns, inspect
`family_readiness_state`, `family_readiness_reason`,
`current_publication_identity`, `discovered_release_identity`, and
`member_identity_verification` (expected and persisted legal/IP identities,
dates and deterministic mismatch reasons), plus `publication_lifecycle`,
`publication_proof_reasons`, `accepted_descriptor_verdict`, and
`admission_fingerprint`; do not
override a `SAME_RELEASE` family blocker. Any other result stops the run.

## HOME controlled-live package — do not run on Mac

Create exactly one pinned family job:

```bash
PYTHONPATH=. uv run python -m scripts.run_fns_tax_regime_controlled_live \
  enqueue --raw-root "$FNS_RAW_ROOT" \
  --confirm FNS_TAX_REGIME_CONTROLLED_LIVE
```

Expected state: one `fns_tax_regime_release` job, handler
`tax-regime-family-official-v1`, status `queued`, `controlled_live=true`, two
release members and no scheduler activation. If the exact bundle is already
accepted, the command creates/reuses a `fns_tax_regime_check` replay job
instead.

Execute only the admitted family and source-control lane:

```bash
PYTHONPATH=. uv run python -m scripts.run_data_readiness_scheduler \
  --controlled-start \
  --allow-source fns_tax_regime \
  --allow-lane source_control \
  --work-budget 1
```

Expected terminal state: one Worker run `succeeded`; two RAW manifests; one
accepted family descriptor; both child datasets share the accepted data date;
family/member currentness is conservative; source/parsed counts reconcile;
invalid, quarantined and unknown code counts are zero; published snapshots
equal matched unique INNs; no facts exist under the wrong member dataset.

Do not add another source, lane or work unit. Do not activate a scheduler.

## PostgreSQL acceptance queries

Run in a read-only transaction after success:

```sql
SELECT source_id, handler_version, approved, enabled, live_mode
FROM worker_handler_registrations
WHERE source_id = 'fns_tax_regime';

SELECT id, source_id, job_type, handler_version, status,
       schedule_metadata->>'release_identity' AS bundle_identity,
       schedule_metadata->>'controlled_live' AS controlled_live
FROM worker_jobs
WHERE source_id = 'fns_tax_regime'
ORDER BY created_at DESC LIMIT 3;

SELECT r.id, r.status, r.records_seen, r.records_written,
       r.records_rejected, r.records_duplicated, r.records_published,
       r.errors
FROM worker_runs r
JOIN worker_jobs j ON j.id = r.job_id
WHERE j.source_id = 'fns_tax_regime'
ORDER BY r.started_at DESC LIMIT 3;

SELECT source_id, generation, active_pointer, validation_metadata
FROM worker_publication_state
WHERE source_id = 'fns_tax_regime';

SELECT c.company_id, c.source_id, c.status, c.execution_status,
       c.publication_generation, c.source_data_date, c.replay_pointer,
       c.replay_checksum, c.source_snapshot->>'release_identity' AS release_identity,
       c.fact_count, c.checked_at
FROM company_source_coverage c
WHERE c.source_id = 'fns_tax_regime'
ORDER BY c.company_id, c.updated_at DESC;

SELECT code, enabled, operational_status, last_success_at, last_data_date,
       source_as_of, retrieved_at, published_at, record_count, coverage,
       official_actual_until, next_expected_update_at
FROM data_sets
WHERE code IN ('fns_tax_regime', 'fns_snr', 'fns_snrip')
ORDER BY code;

SELECT d.code, count(*) AS snapshots,
       count(DISTINCT (s.company_id, s.data_date)) AS unique_snapshots,
       min(s.data_date) AS min_data_date, max(s.data_date) AS max_data_date
FROM company_tax_regime_snapshots s
JOIN data_sets d ON d.id = s.dataset_id
WHERE d.code IN ('fns_snr', 'fns_snrip')
GROUP BY d.code ORDER BY d.code;

SELECT count(*) AS applicability_conflicts
FROM company_tax_regime_snapshots s
JOIN companies c ON c.id = s.company_id
JOIN data_sets d ON d.id = s.dataset_id
WHERE (d.code = 'fns_snr' AND c.entity_type NOT IN ('legal'))
   OR (d.code = 'fns_snrip' AND c.entity_type NOT IN ('individual_entrepreneur'));

SELECT count(*) AS duplicate_rows
FROM (
  SELECT company_id, dataset_id, data_date, count(*)
  FROM company_tax_regime_snapshots
  GROUP BY company_id, dataset_id, data_date
  HAVING count(*) > 1
) q;
```

For replay acceptance, add one controlled test Company whose exact INN is in
the accepted artifact, enqueue the repository's frozen-snapshot enrichment
replay for that Company, and verify one new snapshot. Repeat the exact replay
and verify zero additional rows. Do not re-download or edit RAW.

## Stop conditions

Stop without publication on any of the following:

- passport identifier/source-page mismatch;
- canonical URL authority/path ambiguity, traversal or malformed encoding;
- mandatory sibling in the discovered release, or persisted `SAME_RELEASE`
  family, unavailable, stale, parse-failed or release incompatible (an old
  `NEW_RELEASE` generation's state is diagnostic only);
- F-C6-134-04: `SAME_RELEASE` preflight family readiness is not current or
  the persisted family identity/date differs from the accepted bundle;
- F-C6-134-05: any SAME_RELEASE identity-chain edge differs from fresh official
  discovery, including coordinated wrong family/child legal or IP identities;
- unproven Company negative, stale Company coverage or changed frozen release;
- ZIP or XSD outside the pinned official dataset path;
- artifact/XSD structure-version mismatch;
- HTTP size, advertised checksum, immutable checksum or replay checksum
  mismatch;
- missing Legal/IP member or different member source dates;
- unknown schema/version, malformed ZIP/XML or XSD validation failure;
- any unknown regime code (threshold zero for the first controlled live);
- invalid INN/OGRNIP/document identity or conflicting entity applicability;
- publication transaction failure or DB constraint error;
- lease/heartbeat/fencing failure;
- artifact size materially above preflight, estimated peak resource breach,
  less than 5 GiB remaining, or less than 10% disk remaining;
- any unexpected claim outside `fns_tax_regime` or more than one work unit.

## Rollback and recovery

Download/parser/publication failures leave the previously accepted family
pointer and facts intact because publication and pointer advancement are one
transaction. Retain immutable RAW and the failed run; do not overwrite or
delete it. Fix the classified cause, repeat read-only preflight, and allow the
same idempotent job to retry under Worker fencing.

If a semantic defect is discovered after a committed run, do not enable the
scheduler or publish publicly. Freeze C6, retain the evidence, open an
incident, and restore the previously accepted generation/facts through an
explicit reviewed PostgreSQL recovery transaction or a corrected immutable
release replay. Never hand-edit current rows without restoring the matching
`WorkerPublicationState` generation and provenance.

## Known limitations

- Risk/Summary rule integration: `NOT INTEGRATED`; no rule is invented here.
- The current card keeps a compatibility adapter while Company View is the
  canonical semantic/Monitoring representation.
- Live source availability and record counts can change after this evidence;
  HOME preflight is authoritative for the execution moment.
- Production scheduling, mass ingestion, public automatic release and final
  legal acceptance remain separate decisions.
