# Product Recovery — factual source status

Date: 2026-09-17
Status: **IN PROGRESS**

Evidence labels follow `EVIDENCE_REPORTING_POLICY.md`.

## Product Recovery v3 current run

`VERIFIED_RUNTIME`, 40-company acceptance run and subsequent Mac control point:

- 40/40 unique companies processed; buckets were formed, but Product Acceptance did not pass.
- Coverage: minimum 47/100, maximum 80/100, average 58/100.
- Positive gate: 0/40. This is an acceptance blocker, not a cosmetic limitation.
- Mac control point at `3ed0506ce372eaaf7bb432b6f3093a137c0a1bf5`: 639 passed, Alembic `a7b8c9d0e1f2 (head)`, local SHA equals origin SHA, clean diff/worktree.

| Capability | Code state | Runtime unresolved in 40 |
|---|---|---:|
| Arbitration | Resolver exists | 39 |
| General Courts | Partial targeted foundation exists | 40 |
| CBR ZSK | Runner foundation exists | 40 |
| FNS Bankinform | Runner foundation exists | 40 |
| FSSP | Direct runner foundation exists | 24 |
| Bankruptcy direct / EFRSB | Runner foundation exists | 21 |

Code state and runtime completion are intentionally separate. A runner existing
in the repository does not prove that its source completed the 40-company run.

## Source classification and authorization

- Firmoteka is a first-class `AUTHORIZED_BRIDGE`, not a Source of Truth.
- Precedence is `OFFICIAL_DIRECT > OFFICIAL_DOWNLOADED_DATASET > AUTHORIZED_BRIDGE > DISCOVERY_ONLY`.
- Authorization for Firmoteka automated parsing and FSSP, EFRSB/Fedresurs and CBR ZSK automated public flows is recorded only as `USER_REPORTED`. No contract or authorization letter was independently inspected.
- For INN `0274101890`, Firmoteka reported 74 proceedings and FSSP direct reported 96; classification is `MISMATCH`. One company is insufficient for a general conclusion about Firmoteka FSSP quality.

## Runtime population audit

`VERIFIED_RUNTIME`, local PostgreSQL, 2026-09-17:

| Measure | Observed |
|---|---:|
| Companies | 6,781,487 |
| Distinct INN | 6,781,487 |
| Status unknown | 6,781,485 |
| ACTIVE | 1 |
| LIQUIDATED | 1 |
| Entity type unknown | 2 |
| Legal entities | 2,043,659 |
| Individual entrepreneurs | 4,737,826 |
| Registration date unknown | 6,781,487 |
| Termination date unknown | 6,781,487 |
| Address unknown | 6,758,012 |
| Region unknown | 14,433 |
| OKVED unknown | 15,511 |
| Activity unknown | 1,094 |
| Master data date unknown | 14,433 |

Master source distribution: `fns_msp` 6,767,054; no master dataset 14,433.
This proves that the SME registry was used as master identity enrichment even
though it does not provide full EGRUL/EGRIP registration state.

## Domain facts

`VERIFIED_RUNTIME`, local PostgreSQL, 2026-09-17:

| Domain | Records | Companies | Data date / limitation |
|---|---:|---:|---|
| Legal events | 0 | 0 | no bankruptcy facts |
| Active bankruptcy | 0 | 0 | not connected |
| Tax debt snapshots | 625,395 | 625,395 | 01.08.2026 |
| Tax offences | 24,139 | 24,139 | 31.12.2024 |
| Revenue/expense | 1,753,134 | 1,753,134 | 2025 |
| General-court checks | 1 | 1 | targeted Moscow coverage |
| Arbitration checks | 2 | 2 | on demand |
| Protected checks | 2 | 1 | one CBR ZSK and one FNS Bankinform |
| CompanySourceData | 3 | 3 | one source only |

## P0 sources

| Source | Current state | Evidence and next requirement |
|---|---|---|
| FNS EGRUL/EGRIP | `ACCESS_PENDING` | `VERIFIED_FROM_GIT`: datasets exist but are `not_configured`. `EXTERNAL_HISTORICAL_EVIDENCE`: [FNS integration documentation](https://www.nalog.gov.ru/rn77/service/egrip2/egrip_vzayim/) describes full annual files plus daily replacement records and requires access attributes. Obtain official integration access; then ingest exact INN/OGRN with replacement semantics. |
| Fedresurs/EFRSB | `ACCESS_PENDING` | `VERIFIED_FROM_GIT`: dataset `fedresurs_messages` is `not_configured`; database has zero legal events. Official [REST specification](https://download.fedresurs.ru/HELP/3.%20BANKRUPT/10.%20MS/10.3%20SERVICE_REST/Service_rest_1.0.2.pdf) documents authorized endpoints. Production credentials/access are absent from the safe environment. |
| FSSP | `HUMAN_ACTION_REQUIRED` foundation | `VERIFIED_FROM_GIT`: protected session flow now has the official [Bank of enforcement proceedings](https://fssp.gov.ru/iss/ip/) URL and fail-closed result states. No completed FSSP company result exists; CAPTCHA/manual execution remains required. |
| Checko arbitration | `USER_TRIGGERED` | Provider exists. Credential must come only from environment. Absence of a credential is an explicit error, never `not_found`. |
| General courts | `PARTIAL_COVERAGE` | Moscow-only targeted path; never represented as nationwide coverage or business risk. |

## Hard blockers to acceptance

1. Operationalize FSSP, EFRSB, CBR ZSK and Bankinform runtime paths.
2. Resolve Arbitration for 40/40 companies and improve General Courts coverage.
3. Rerun the same 40-company regression and raise mandatory Coverage.
4. Open the positive gate from its current 0/40 only after sufficient Coverage and all mandatory checks are closed.
