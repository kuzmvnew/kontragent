# Day 0 — S04 / S03 / common supervisor evidence

Evidence timestamp: 2026-09-24 (UTC unless noted).

## What is implemented

- S04 `fns_tax_offence` and S03 `fns_revenue_expenses` are distinct Worker
  Foundation sources. They have different handler versions, idempotency
  namespaces, one-source leases, run histories and publication generations.
- Each official-release handler performs official passport discovery, pinned
  ZIP/XSD download, HTTP-length and advertised-checksum validation when the
  publisher provides one, immutable checksum-addressed RAW storage, XSD+ZIP
  validation, streaming normalization and staging.
- The existing domain parsers remain authoritative. The publisher exact-matches
  ten-digit INNs to `companies`, replaces the source snapshot and upserts facts
  in the same Worker success transaction. A failure therefore preserves the
  previous facts, dataset last-success metadata and active publication pointer.
- Existing tax-offence and revenue/expense service/card projections are reused.
  Publication metadata records the count of Risk/Summary candidate companies;
  this source job does not eagerly rewrite risk/summary assessments.
- The existing scheduler now has non-empty handlers for both datasets (S04 is
  ordered first), daily release *checks*, explicit activation, durable
  `next_expected_update_at`, a check-only Worker run when no new release exists,
  Worker retry/backoff, and a dataset error projection that does not clear the
  last success.

## Official discovery and bounded evidence

### S04 — tax offence

- Official passport:
  `https://www.nalog.gov.ru/opendata/7707329152-taxoffence/`
- Current ZIP:
  `data-20251201-structure-20191201.zip`
- Source data date: `2024-12-31` (kept separately from schedule/check time).
- Passport actual-until: `2026-12-01`.
- Retrieved immutable ZIP: `4,878,806` bytes,
  SHA-256 `1a388022e0db361dc1cc78d65b4eb6f5f08a1d1fc59a9c6d035e1e8b4b4e384b`.
- XSD: `structure-20181201.xsd`, `11,214` bytes,
  SHA-256 `33aeb64fcedb2cbd63f5e752e2a406bcce3901347128664fa0d48b689ac5b441`.
- Real ZIP CRC + XSD + parser result: `111` XML files, `100,005` seen,
  `100,005` valid, `0` rejected, one data date.
- Read-only exact-INN preflight against the current local master:
  `24,273` matched records/companies and `75,732` unmatched. No facts were
  written by this Day 0 preflight.

### S03 — REVEXP

- Official passport:
  `https://www.nalog.gov.ru/opendata/7707329152-revexp/`
- Current ZIP: `data-20260825-structure-20180110.zip`.
- Source data date: `2025-12-31`; passport actual-until: `2026-09-25`.
- Read-only official response metadata: `104,542,676` bytes; publisher SHA-256
  `d09e9fedc3df1118c1a53891e86ea4564f7f1f83c9cd983e5cc771669c0597ab`.
- Current XSD: `structure-20180110.xsd`, `11,651` bytes; publisher SHA-256
  `71156f70e08072672d7cc28bb7dd8e9f03891edb72411133b23d4c1dabcd8f57`.
- The 104.5 MB archive was deliberately not downloaded or ingested during Day
  0. A full national ingestion would violate the bounded-run instruction.

## Existing database/read projection baseline

The local `kontragent` database was inspected read-only:

| Source | Existing facts | Last source date | Last success | Schedule state |
|---|---:|---|---|---|
| S04 | 24,139 | 2024-12-31 | 2026-09-14 00:27:14 +05 | disabled / not_configured |
| S03 | 1,753,134 | 2025-12-31 | 2026-09-15 00:10:52 +05 | disabled / not_configured |

For company `3812993020`, direct read projections returned `found` for both
S04 (`2024-12-31`, one document) and S03 (`2025-12-31`). Full Company Card
aggregation on that database is currently blocked before these sections by an
unrelated schema baseline mismatch: the merged S02 code expects
`fns_tax_debt_pilot_state`, which has not been migrated into that database.

Neither source is labelled operational by this change. Day 0 does not provide
a new successful Worker run followed by a scheduled no-new-release run, and the
schedules remain behind the explicit `--activate-s03-s04` operator gate.

## Verification

- S03/S04 plus Worker Foundation targeted suite: `70 passed` on the isolated
  PostgreSQL database `test`.
- Fresh disposable database `day0_c_20260924`: `alembic upgrade head` passed.
- The S02 disposable-operator test now freezes the child pipeline clock as well
  as the operator and Worker clocks. This is a test-only follow-up to merged
  PR #49; production code and the queue guard are unchanged.
- Full suite on that fresh database: `987 passed`, `2 warnings`.

## Matrix queue scope

`docs/source_execution_queue.csv` is a machine-readable, process-deduplicated
seed queue. The original 214-position workbook is not present in Git (as already
recorded in `MASTER_SOURCE_MATRIX.md`), so this seed must not be described as a
complete reconstruction of all 214 positions. It starts with repository-backed
unique source processes and can be reconciled against the canonical workbook
when that artifact is supplied.
