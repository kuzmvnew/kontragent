# Product Recovery — factual source status

Date: 2026-09-17
Status: **IN PROGRESS**

Evidence labels follow `EVIDENCE_REPORTING_POLICY.md`.

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

1. Official EGRUL/EGRIP integration access and full files are not available in the environment.
2. Fedresurs production REST access is not available; zero bankruptcy records exist.
3. No FSSP checks have been manually completed for acceptance companies.
4. Therefore the 10 bankruptcy and 10 low-risk/high-coverage buckets cannot yet be populated truthfully.
